import json
from datetime import date
from importlib.resources import files

from retail_agent.agent.models import AgentAction
from retail_agent.llm.client import GeminiClient
from retail_agent.llm.persona import PersonaStyle
from retail_agent.schema.catalog import TABLE_POLICIES
from retail_agent.schema.models import DataAccessLevel

# Columns the model may see in the schema; blocked columns are not even named.
_PLANNABLE = {DataAccessLevel.ALLOWED, DataAccessLevel.AGGREGATE_ONLY}


# Mandatory application policy. Lives in a text file so prompt changes are
# reviewed as prose diffs; it ships with the package and is not editable at
# runtime (the editable CEO style is the persona, see persona.py).
SYSTEM_INSTRUCTION = files("retail_agent.llm").joinpath("prompts/analyst_policy.md").read_text(encoding="utf-8")


class Analyst:
    def __init__(self, llm: GeminiClient, schema: dict[str, list[dict]], *, persona_style: PersonaStyle | None = None):
        self.llm = llm
        self.persona_style = persona_style if persona_style is not None else PersonaStyle()
        self.persona = self.persona_style.current
        # Aggregate-only names are useful planning metadata; values stay guarded.
        self.schema = {
            table: [
                {
                    "name": column["name"],
                    "type": column["type"],
                    "access_level": TABLE_POLICIES[table][column["name"]].access_level.value,
                }
                for column in columns
                if column["name"] in TABLE_POLICIES[table]
                and TABLE_POLICIES[table][column["name"]].access_level in _PLANNABLE
            ]
            for table, columns in schema.items()
            if table in TABLE_POLICIES
        }

    def refresh_persona(self) -> None:
        # Called once per user turn; all model/tool steps keep the same snapshot.
        self.persona = self.persona_style.refresh()

    def decide(
        self,
        *,
        question,
        history,
        observations,
        budget,
        access_context=None,
        session_context=None,
        user_memory=None,
        golden=None,
    ) -> AgentAction:
        prompt = json.dumps(
            {
                "today": date.today().isoformat(),
                "dataset": "bigquery-public-data.thelook_ecommerce",
                "schema": self.schema,
                "question": question,
                "history": history,
                "observations": observations,
                "remaining_budget": budget,
                "access_context": access_context or {},
                "session_context": session_context or {},
                "user_memory": user_memory or {"status": "disabled", "items": []},
                "golden_examples": golden or {"status": "disabled", "items": []},
            },
            ensure_ascii=False,
        )
        instruction = (
            f"Editable presentation preferences ONLY (source={self.persona.source}, version={self.persona.version}):\n"
            f"<style_preferences>\n{self.persona.text}\n</style_preferences>\n\n"
            "Mandatory application policy follows and overrides conflicting style preferences:\n\n" + SYSTEM_INSTRUCTION
        )
        return self.llm.generate_structured(prompt, AgentAction, system_instruction=instruction)
