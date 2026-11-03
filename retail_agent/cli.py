"""Command-line entry point: arguments, startup messages and the chat input loop."""

import argparse

from pydantic import ValidationError

from retail_agent.agent.orchestrator import AnalyticalAgent
from retail_agent.application import ChatApplication, ChatOptions, StartupError
from retail_agent.settings import Settings, invalid_settings


def parse_options(settings: Settings, argv=None) -> ChatOptions:
    parser = argparse.ArgumentParser(description="Brand-scoped retail analysis chat")
    parser.add_argument("--project", default=settings.google_cloud_project)
    parser.add_argument("--manager", default=settings.retail_manager_id)
    parser.add_argument("--access-config", default="access.local.json")
    parser.add_argument("--log-file", default="logs/agent.jsonl")
    parser.add_argument("--min-cohort-size", type=int, default=5)
    args = parser.parse_args(argv)
    if args.min_cohort_size < 2:
        parser.error("--min-cohort-size must be at least 2")
    return ChatOptions(
        project=args.project,
        manager=args.manager,
        access_config=args.access_config,
        log_file=args.log_file,
        min_cohort_size=args.min_cohort_size,
    )


def run_chat(agent: AnalyticalAgent) -> None:
    print("Retail analyst. Commands: /exit, /reset, /help, /confirm, /cancel, /memories, /forget ID, /forget-all")
    print("Demo identity is fixed at startup. Do not enter personal contact details.")
    while True:
        try:
            question = input("\nYou: ").strip()
            if question.lower() in {"/exit", "/quit"}:
                return
            if question.lower() == "/reset":
                agent.reset()
                print("Conversation cleared.")
                continue
            if question.lower() == "/help":
                print("Ask about products, customer spending by ID, or sales over time.")
                print("Demographics use aggregate SQL with computed groups; small cohorts are suppressed.")
                print("Ask to save the last answer, find/list/open your reports, or preview deletion.")
                print("Deletion requires a separate /confirm; /cancel or any new question cancels it.")
                print(
                    "Explicitly ask to remember preferences; /memories lists them, /forget ID or /forget-all removes them."
                )
                continue
            if not question:
                continue
            result = agent.ask(question)
            print(f"\nAnalyst: {result.answer}")
            print(f"[SQL attempts: {result.queries}; model calls: {result.model_calls}]")
        except (EOFError, KeyboardInterrupt):
            print("\nSession ended.")
            return
        except Exception:
            # Last UI boundary: do not print backend exception text or traceback.
            print("This request could not be completed. You can ask another question.")


def main(argv=None) -> int:
    try:
        settings = Settings()
    except ValidationError as error:
        print(invalid_settings(error))
        return 1
    options = parse_options(settings, argv)
    with ChatApplication(settings, options) as app:
        try:
            agent = app.start()
        except StartupError as error:
            print(error)
            return 1
        run_chat(agent)
    return 0
