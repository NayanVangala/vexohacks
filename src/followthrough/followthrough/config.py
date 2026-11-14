"""All settings come from environment variables. Read at call time so tests can change them."""
import os
from dataclasses import dataclass


@dataclass
class Config:
    openai_api_key: str      # OPENAI_API_KEY (the only thing you must add)
    model: str               # OPENAI_MODEL, chat model used to find commitments (needs structured output)
    transcribe_model: str    # OPENAI_TRANSCRIBE_MODEL, must give segment timestamps (whisper-1 does)
    wearer: str              # WEARER_NAME, who the agent works for
    timezone: str            # TIMEZONE, used to resolve "tomorrow at 3"
    executor_url: str        # EXECUTOR_URL, Google Apps Script web app that really sends and schedules
    executor_token: str      # EXECUTOR_TOKEN, shared secret checked by the Apps Script
    email_to: str            # DEMO_EMAIL_TO, every email goes here no matter what the model says
    calendar_account: str    # CALENDAR_ACCOUNT, only shown in the UI; the Apps Script uses its own default calendar
    data_dir: str            # FOLLOWTHROUGH_DATA, where state.json is saved


def get_config() -> Config:
    return Config(
        openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
        model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
        transcribe_model=os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1"),
        wearer=os.environ.get("WEARER_NAME", "the wearer"),
        timezone=os.environ.get("TIMEZONE", "America/Los_Angeles"),
        executor_url=os.environ.get("EXECUTOR_URL", "").strip(),
        executor_token=os.environ.get("EXECUTOR_TOKEN", "").strip(),
        email_to=os.environ.get("DEMO_EMAIL_TO", "ultrasrijucat970@gmail.com").strip(),
        calendar_account=os.environ.get("CALENDAR_ACCOUNT", "srijansaha.work@gmail.com").strip(),
        data_dir=os.environ.get("FOLLOWTHROUGH_DATA", "data"),
    )
