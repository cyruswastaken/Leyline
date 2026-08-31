import os

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()


AZURE_OPENAI_ENDPOINT = os.getenv(
    "AZURE_OPENAI_ENDPOINT"
)

AZURE_OPENAI_API_KEY = os.getenv(
    "AZURE_OPENAI_API_KEY"
)

AZURE_OPENAI_DEPLOYMENT = os.getenv(
    "AZURE_OPENAI_DEPLOYMENT"
)


if not AZURE_OPENAI_ENDPOINT:
    raise RuntimeError(
        "AZURE_OPENAI_ENDPOINT is not configured"
    )

if not AZURE_OPENAI_API_KEY:
    raise RuntimeError(
        "AZURE_OPENAI_API_KEY is not configured"
    )

if not AZURE_OPENAI_DEPLOYMENT:
    raise RuntimeError(
        "AZURE_OPENAI_DEPLOYMENT is not configured"
    )


client = OpenAI(
    base_url=(
        f"{AZURE_OPENAI_ENDPOINT.rstrip('/')}"
        "/openai/v1/"
    ),
    api_key=AZURE_OPENAI_API_KEY
)


def generate_answer(
    query: str,
    context: str,
    source: str
) -> str:

    if not context:
        return (
            "I couldn't find relevant information "
            "in the available knowledge base."
        )

    system_prompt = """
You are Leyline, a secure enterprise IT helpdesk assistant.

Your job is to answer employee questions using ONLY
the provided knowledge-base context.

Rules:

1. Do not invent information.
2. If the answer is not supported by the context,
   clearly say that the information is unavailable.
3. Never request or expose passwords, MFA codes,
   API keys, or other credentials.
4. Keep answers concise and practical.
5. When useful, present instructions as numbered steps.
6. Mention the knowledge-base source at the end.
"""

    user_prompt = f"""
Knowledge-base context:

{context}

Employee question:

{query}

Knowledge-base source:

{source}
"""

    response = client.responses.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        instructions=system_prompt,
        input=user_prompt
    )

    return response.output_text