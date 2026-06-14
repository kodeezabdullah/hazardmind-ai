import os
from pathlib import Path
from openai import AsyncOpenAI
from dotenv import load_dotenv


def load_agent_env():
    root_env = Path(__file__).resolve().parents[2] / '.env'
    load_dotenv(root_env)

    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        agent_env = parent / '.env'
        if agent_env.exists() and agent_env != root_env:
            load_dotenv(agent_env, override=True)
            break


load_agent_env()

FALLBACK_MODELS = [
    "google/gemma-4-31B-it",
    "moonshotai/Kimi-K2.6",
    "google/gemma-3-27b-it",
    "Qwen/Qwen3-35B-A22B",
]

AIML_MODEL = "claude-opus-4-8"

featherless_client = AsyncOpenAI(
    api_key=os.getenv("FEATHERLESS_API_KEY"),
    base_url="https://api.featherless.ai/v1"
)

aiml_client = AsyncOpenAI(
    api_key=os.getenv("AIML_API_KEY"),
    base_url="https://api.aimlapi.com/v1"
)

async def call_with_fallback(prompt: str,
                              system: str = "",
                              use_aiml_first: bool = False):
    models = ([AIML_MODEL] if use_aiml_first else []) + FALLBACK_MODELS
    if not use_aiml_first:
        models.append(AIML_MODEL)

    for model in models:
        try:
            client = aiml_client if model == AIML_MODEL else featherless_client
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=2000
            )
            return response.choices[0].message.content
        except Exception as e:
            print(f"Model {model} failed: {e}")
            continue
    raise Exception("All models in fallback chain failed!")
