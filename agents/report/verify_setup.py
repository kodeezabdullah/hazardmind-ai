from pathlib import Path
import os
import importlib.util
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
CONFIG_PATH = BASE_DIR / "agent_config.yaml"

def check(condition, success_message, fail_message, errors):
    if condition:
        print(f"✅ {success_message}")
    else:
        print(f"❌ {fail_message}")
        errors.append(fail_message)

def package_exists(package_name):
    return importlib.util.find_spec(package_name) is not None

def main():
    errors = []

    print("\nHazardMind Report Agent Setup Verification\n")

    check(
        ENV_PATH.exists(),
        ".env file found",
        ".env file is missing",
        errors
    )

    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)

    check(
        bool(os.getenv("AIML_API_KEY")),
        "AIML_API_KEY is set",
        "AIML_API_KEY is missing in .env",
        errors
    )

    check(
        bool(os.getenv("BAND_API_KEY")),
        "BAND_API_KEY is set",
        "BAND_API_KEY is missing in .env",
        errors
    )

    check(
        CONFIG_PATH.exists(),
        "agent_config.yaml found",
        "agent_config.yaml is missing",
        errors
    )

    if CONFIG_PATH.exists():
        config_text = CONFIG_PATH.read_text(encoding="utf-8")

        check(
            "hazardmind-report" in config_text,
            "Agent name hazardmind-report found in config",
            "Agent name hazardmind-report not found in config",
            errors
        )

        check(
            "PASTE_YOUR_BAND_AGENT_UUID_HERE" not in config_text,
            "Band agent UUID appears to be filled",
            "Band agent UUID placeholder is still present",
            errors
        )

    required_packages = {
        "dotenv": "python-dotenv",
        "openai": "openai",
        "asyncpg": "asyncpg",
        "httpx": "httpx",
        "reportlab": "reportlab",
        "jinja2": "jinja2",
        "boto3": "boto3",
    }

    for import_name, package_name in required_packages.items():
        check(
            package_exists(import_name),
            f"{package_name} installed",
            f"{package_name} is not installed",
            errors
        )

    if errors:
        print("\nSetup verification failed. Fix the errors above.\n")
        raise SystemExit(1)

    print("\n✅ Setup verification passed. Report Agent environment is ready.\n")

if __name__ == "__main__":
    main()