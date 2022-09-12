"""Functions related to the loading of models."""

import subprocess
import warnings
from subprocess import CalledProcessError
from typing import Any, Dict

import spacy
from transformers.models.auto.configuration_auto import AutoConfig
from transformers.models.auto.tokenization_auto import AutoTokenizer

from .config import EvaluationConfig, ModelConfig, TaskConfig
from .enums import Framework
from .exceptions import InvalidEvaluation, InvalidFramework, ModelFetchFailed
from .model_adjustment import adjust_model_to_task
from .utils import check_supertask, get_class_by_name, is_module_installed

# Ignore warnings from spaCy. This has to be called after the import, as the
# __init__.py file of spaCy sets the warning levels of spaCy warning W036
warnings.filterwarnings("ignore", module="spacy*")


def load_model(
    model_config: ModelConfig,
    task_config: TaskConfig,
    evaluation_config: EvaluationConfig,
) -> Dict[str, Any]:
    """Load the model.

    Args:
        model_config (ModelConfig):
            The model configuration.
        task_config (TaskConfig):
            The task configuration.
        evaluation_config (EvaluationConfig):
            The evaluation configuration.

    Returns:
        dict:
            A dictionary containing at least the key 'model', with the value being the
            model. Can contain other objects related to the model, such as its
            tokenizer.

    Raises:
        InvalidFramework:
            If the framework is not recognized.
    """
    # Ensure that the framework is installed
    from_flax = model_config.framework == Framework.JAX

    # If the framework is JAX then change it to PyTorch, since we will convert JAX
    # models to PyTorch upon download
    if model_config.framework == Framework.JAX:
        model_config.framework = Framework.PYTORCH

    if model_config.framework == Framework.PYTORCH:
        return load_pytorch_model(
            model_config=model_config,
            from_flax=from_flax,
            task_config=task_config,
            evaluation_config=evaluation_config,
        )

    elif model_config.framework == Framework.SPACY:
        return load_spacy_model(model_id=model_config.model_id)

    else:
        raise InvalidFramework(model_config.framework)


def load_pytorch_model(
    model_config: ModelConfig,
    from_flax: bool,
    task_config: TaskConfig,
    evaluation_config: EvaluationConfig,
) -> Dict[str, Any]:
    """Load a PyTorch model.

    Args:
        model_config (ModelConfig):
            The configuration of the model.
        from_flax (bool):
            Whether the model is a Flax model.
        task_config (TaskConfig):
            The task configuration.
        evaluation_config (EvaluationConfig):
            The evaluation configuration.

    Returns:
        dict:
            A dictionary containing at least the key 'model', with the value being the
            model. Can contain other objects related to the model, such as its
            tokenizer.

    Raises:
        InvalidEvaluation:
            If the model either does not have any registered frameworks, of it is a
            private model and `use_auth_token` has not been set, or if the supertask
            does not correspond to a Hugging Face AutoModel class.
    """
    try:
        # Load the configuration of the pretrained model
        config = AutoConfig.from_pretrained(
            model_config.model_id,
            revision=model_config.revision,
            use_auth_token=evaluation_config.use_auth_token,
        )

        # Check whether the supertask is a valid one
        supertask = task_config.supertask
        check_supertask(architectures=config.architectures, supertask=supertask)

        # Get the model class associated with the supertask
        model_cls = get_class_by_name(
            class_name=f"auto-model-for-{supertask}",
            module_name="transformers",
        )

        # If the model class could not be found then raise an error
        if not model_cls:
            raise InvalidEvaluation(
                f"The supertask '{supertask}' does not correspond to a Hugging Face "
                " AutoModel type (such as `AutoModelForSequenceClassification`)."
            )

        # Load the model with the correct model class
        model = model_cls.from_pretrained(  # type: ignore[attr-defined]
            model_config.model_id,
            revision=model_config.revision,
            use_auth_token=evaluation_config.use_auth_token,
            config=config,
            cache_dir=evaluation_config.cache_dir,
            from_flax=from_flax,
        )

    # If an error occured then throw an informative exception
    except (OSError, ValueError):
        raise InvalidEvaluation(
            f"The model {model_config.model_id} either does not have a frameworks "
            "registered, or it is a private model. If it is a private model then "
            "enable the `--use-auth-token` flag and make  sure that you are "
            "logged in to the Hub via the `huggingface-cli login` command."
        )

    # Ensure that the model is compatible with the task
    adjust_model_to_task(
        model=model,
        model_config=model_config,
        task_config=task_config,
    )

    # If the model is a subclass of a RoBERTa model then we have to add a prefix space
    # to the tokens, by the way the model is constructed.
    m_id = model_config.model_id
    prefix = "Roberta" in type(model).__name__
    params = dict(use_fast=True, add_prefix_space=prefix)
    tokenizer = AutoTokenizer.from_pretrained(
        m_id,
        revision=model_config.revision,
        use_auth_token=evaluation_config.use_auth_token,
        **params,
    )

    # Set the maximal length of the tokenizer to the model's maximal length. This is
    # required for proper truncation
    if not hasattr(tokenizer, "model_max_length") or tokenizer.model_max_length > 1_000:

        if hasattr(tokenizer, "max_model_input_sizes"):
            all_max_lengths = tokenizer.max_model_input_sizes.values()
            if len(list(all_max_lengths)) > 0:
                min_max_length = min(list(all_max_lengths))
                tokenizer.model_max_length = min_max_length
            else:
                tokenizer.model_max_length = 512
        else:
            tokenizer.model_max_length = 512

    # Set the model to evaluation mode, making its predictions deterministic
    model.eval()

    # Move the model to the specified device
    model.to(evaluation_config.device)

    return dict(model=model, tokenizer=tokenizer)


def load_spacy_model(model_id: str) -> Dict[str, Any]:
    """Load a spaCy model.

    Args:
        model_id (str):
            The ID of the model.

    Returns:
        dict:
            A dictionary containing at least the key 'model', with the value being the
            model. Can contain other objects related to the model, such as its
            tokenizer.

    Raises:
        ModelFetchFailed:
            If the model could not be downloaded.
    """
    local_model_id = model_id.split("/")[-1]

    # Download the model if it has not already been so
    try:
        if not is_module_installed(local_model_id):
            url = (
                f"https://huggingface.co/{model_id}/resolve/main/{local_model_id}-"
                "any-py3-none-any.whl"
            )
            subprocess.run(["pip3", "install", url])

    except CalledProcessError as e:
        raise ModelFetchFailed(model_id=local_model_id, error_msg=e.output)

    # Load the model
    try:
        model = spacy.load(local_model_id)
    except OSError as e:
        raise ModelFetchFailed(
            model_id=model_id,
            error_msg=str(e),
            message=(
                f"Download of {model_id} failed, with the following error message: "
                f"{str(e)}."
            ),
        )
    return dict(model=model)
