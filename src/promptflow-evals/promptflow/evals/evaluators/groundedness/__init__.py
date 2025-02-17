# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------

__path__ = __import__("pkgutil").extend_path(__path__, __name__)  # type: ignore

from promptflow import load_flow
from promptflow.entities import AzureOpenAIConnection
from pathlib import Path


class GroundednessEvaluator:
    def __init__(self, model_config: AzureOpenAIConnection, deployment_name: str):
        """
        Initialize an evaluator configured for a specific Azure OpenAI model.

        :param model_config: Configuration for the Azure OpenAI model.
        :type model_config: AzureOpenAIConnection
        :param deployment_name: Deployment to be used which has Azure OpenAI model.
        :type deployment_name: AzureOpenAIConnection

        **Usage**

        .. code-block:: python

            eval_fn = GroundednessEvaluator(model_config, deployment_name="gpt-4")
            result = eval_fn(
                answer="The capital of Japan is Tokyo.",
                context="Tokyo is Japan's capital, known for its blend of traditional culture \
                    and technological advancements.")
        """

        # Load the flow as function
        current_dir = Path(__file__).resolve().parent
        flow_dir = current_dir / "flow"
        self._flow = load_flow(source=flow_dir)

        # Override the connection
        self._flow.context.connections = {
            "query_llm": {
                "connection": AzureOpenAIConnection(
                    api_base=model_config.api_base,
                    api_key=model_config.api_key,
                    api_version=model_config.api_version,
                    api_type="azure"
                ),
                "deployment_name": deployment_name,
            }
        }

    def __call__(self, *, answer: str, context: str, **kwargs):
        """Evaluate groundedness of the answer in the context.

        :param answer: The answer to be evaluated.
        :type answer: str
        :param context: The context in which the answer is evaluated.
        :type context: str
        :return: The groundedness score.
        :rtype: dict
        """

        # Run the evaluation flow
        return self._flow(answer=answer, context=context)
