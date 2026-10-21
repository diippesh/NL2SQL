import logging
import os
from typing import Optional

from dotenv import load_dotenv
from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.prompts.chat import HumanMessagePromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, ConfigDict, Field

load_dotenv()

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "60"))

if not GEMINI_API_KEY:
    raise EnvironmentError(
        "Missing GEMINI_API_KEY. Add it to your .env file: GEMINI_API_KEY=your-key-here"
    )


class SQLResponse(BaseModel):
    """Structured response produced by the LangChain SQL generation chain."""

    model_config = ConfigDict(extra="ignore")

    query: str = Field(description="A read-only SELECT or WITH query, or an empty string.")
    error: Optional[str] = Field(
        default=None, description="Why SQL generation failed, or null on success."
    )
    reasoning: str = Field(description="One or two concise sentences explaining the query.")
    chart_suggestion: str = Field(
        default="None",
        description=(
            "One of Bar Chart, Line Chart, Scatter Plot, Area Chart, Histogram, "
            "Pie Chart, or None."
        ),
    )


def build_sql_chain(system_message: str):
    """Build an LCEL prompt-to-structured-response chain for one schema."""
    prompt = ChatPromptTemplate.from_messages(
        [
            SystemMessage(content=system_message),
            HumanMessagePromptTemplate.from_template("{user_message}"),
        ]
    )
    llm = ChatGoogleGenerativeAI(
        model=GEMINI_MODEL,
        google_api_key=GEMINI_API_KEY,
        temperature=0,
        timeout=GEMINI_TIMEOUT_SECONDS,
        max_retries=1,
    )
    return prompt | llm.with_structured_output(SQLResponse)


def generate_sql_response(system_message: str, user_message: str) -> SQLResponse:
    """Run the LangChain LCEL SQL generation chain."""
    try:
        logger.info("Generating SQL with LangChain model: %s", GEMINI_MODEL)
        chain = build_sql_chain(system_message)
        result = chain.invoke({"user_message": user_message})
        if not isinstance(result, SQLResponse):
            raise TypeError(f"Unexpected structured response type: {type(result).__name__}")
        return result
    except Exception:
        logger.exception("Error generating SQL with LangChain")
        raise
