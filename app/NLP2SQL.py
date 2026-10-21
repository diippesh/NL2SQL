import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../")))
import io
import json
from pathlib import Path
import re
import logging
from typing import Optional
from time import time

import pandas as pd
import numpy as np
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

import src.database.DB_Config as DB_Config
from src.prompts.Base_Prompt import SYSTEM_MESSAGE
from src.api.LLM_Config import generate_sql_response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

PRIMARY_COLOR = "#6366F1"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SQLITE_DB = PROJECT_ROOT / "data" / "sampledatabase.db"

st.set_page_config(page_icon="\U0001F5C3", page_title="NLP2SQL", layout="wide")

# Small, targeted CSS on top of .streamlit/config.toml's theme -- not a full
# override, just nicer section headers and rounder buttons/inputs.
st.markdown(
    f"""
    <style>
    .stButton>button {{
        border-radius: 8px;
        border: 1px solid {PRIMARY_COLOR};
    }}
    div[data-testid="stMetric"] {{
        background-color: rgba(99, 102, 241, 0.08);
        border-radius: 10px;
        padding: 0.75rem;
    }}
    </style>
    """,
    unsafe_allow_html=True,
)


def section_header(title: str) -> None:
    st.markdown(
        f"""
        <div style="border-left: 4px solid {PRIMARY_COLOR}; padding-left: 0.6rem; margin: 1.3rem 0 0.6rem 0;">
            <span style="font-size: 1.15rem; font-weight: 600;">{title}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


st.title("NLP2SQL")
st.caption("Ask a question in plain English, get back SQL, results, and a chart.")


@st.cache_resource
def load_system_message(schemas: dict) -> str:
    """Format the system prompt with the current database schema."""
    return SYSTEM_MESSAGE.format(schemas=json.dumps(schemas, indent=2))


def validate_sql_query(query: str) -> bool:
    """Only allow safe, read-only SELECT/WITH queries."""
    if not isinstance(query, str) or not query.strip():
        return False

    disallowed_keywords = r"\b(DROP|DELETE|INSERT|UPDATE|ALTER|CREATE|EXEC)\b"
    if re.search(disallowed_keywords, query, re.IGNORECASE):
        return False

    if not query.strip().lower().startswith(("select", "with")):
        return False

    if query.count("(") != query.count(")"):
        return False

    return True


def get_data(query: str, db_name: str, db_type: str, host: Optional[str] = None,
             user: Optional[str] = None, password: Optional[str] = None):
    """Validate and run a query. Returns (DataFrame, error_message)."""
    if not validate_sql_query(query):
        return pd.DataFrame(), "The generated query failed safety validation (only SELECT/WITH queries are allowed)."
    try:
        df = DB_Config.query_database(query, db_name, db_type, host, user, password)
        return df, None
    except Exception as e:
        logger.exception(f"Query execution failed: {e}")
        return pd.DataFrame(), str(e)


def save_temp_file(uploaded_file) -> str:
    temp_file_path = PROJECT_ROOT / "temp_database.db"
    with open(temp_file_path, "wb") as f:
        f.write(uploaded_file.read())
    return str(temp_file_path)


def generate_sql_query(user_message: str, schemas: dict, max_attempts: int = 2) -> dict:
    """Use the LangChain LCEL chain to generate SQL and chart metadata."""
    formatted_system_message = load_system_message(schemas)
    last_error = "Unknown error."

    for attempt in range(max_attempts):
        try:
            parsed = generate_sql_response(formatted_system_message, user_message)
            return {
                "query": parsed.query,
                "error": parsed.error,
                "reasoning": parsed.reasoning,
                "chart_suggestion": parsed.chart_suggestion,
            }
        except Exception as e:
            last_error = str(e)
            logger.exception(f"Unexpected error generating SQL: {e}")
            continue

    return {
        "query": "",
        "error": f"Failed after {max_attempts} attempt(s). Last error: {last_error}",
        "reasoning": "",
        "chart_suggestion": "None",
    }


def create_chart(df: pd.DataFrame, chart_type: str, x_col: str, y_col: str):
    try:
        if chart_type == "Bar Chart":
            fig = px.bar(df, x=x_col, y=y_col, title=f"{y_col} by {x_col}")
        elif chart_type == "Line Chart":
            fig = px.line(df, x=x_col, y=y_col, title=f"{y_col} over {x_col}", markers=True)
        elif chart_type == "Scatter Plot":
            fig = px.scatter(df, x=x_col, y=y_col, title=f"{y_col} vs {x_col}")
        elif chart_type == "Area Chart":
            fig = px.area(df, x=x_col, y=y_col, title=f"{y_col} by {x_col}")
        elif chart_type == "Histogram":
            fig = px.histogram(df, x=x_col, nbins=30, title=f"Distribution of {x_col}")
        elif chart_type == "Pie Chart":
            fig = px.pie(df, names=x_col, values=y_col, title=f"{x_col} share of {y_col}")
        elif chart_type == "Box Plot":
            fig = px.box(df, x=x_col, y=y_col, title=f"{y_col} by {x_col}")
        else:
            return None
        fig.update_layout(autosize=True, margin=dict(l=20, r=20, t=40, b=20))
        return fig
    except Exception as e:
        st.error(f"Could not generate chart: {e}")
        return None


def export_results(df: pd.DataFrame, export_format: str) -> None:
    if export_format == "CSV":
        st.download_button(
            "Download CSV", data=df.to_csv(index=False),
            file_name="query_results.csv", mime="text/csv",
        )
    elif export_format == "Excel":
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Sheet1")
        buf.seek(0)
        st.download_button(
            "Download Excel", data=buf, file_name="query_results.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    elif export_format == "JSON":
        st.download_button(
            "Download JSON", data=df.to_json(orient="records"),
            file_name="query_results.json", mime="application/json",
        )


def handle_query_response(response: dict, db_name: str, db_type: str,
                           host: Optional[str] = None, user: Optional[str] = None,
                           password: Optional[str] = None) -> None:
    query = response.get("query", "")
    error = response.get("error")
    reasoning = response.get("reasoning", "")
    chart_suggestion = response.get("chart_suggestion", "None")

    if error:
        st.error(f"Could not generate a SQL query: {error}")
        return
    if not query:
        st.warning("No query generated. Try rephrasing your question.")
        return

    section_header("Generated SQL")
    st.code(query, language="sql")
    if reasoning:
        st.info(reasoning)

    start_time = time()
    df, run_error = get_data(query, db_name, db_type, host, user, password)
    execution_time = time() - start_time

    if run_error:
        st.error(f"Query failed: {run_error}")
        return

    if df.empty:
        st.info("The query ran successfully but returned no rows.")
        return

    section_header("Results")
    m1, m2 = st.columns(2)
    m1.metric("Rows returned", len(df))
    m2.metric("Execution time", f"{execution_time:.3f}s")

    if len(df) > 10000:
        if st.checkbox("Large result set -- sample 10,000 rows for display/charting?", value=True):
            df = df.sample(10000)
            st.caption("Showing a 10,000-row sample.")

    st.dataframe(df, use_container_width=True, height=450)

    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if numeric_cols:
        with st.expander("Summary Statistics", expanded=False):
            st.dataframe(df[numeric_cols].describe(), use_container_width=True)

    if len(df.columns) >= 2:
        section_header("Visualization")
        columns = df.columns.tolist()
        suggested_x = numeric_cols[0] if numeric_cols else columns[0]
        suggested_y = numeric_cols[1] if len(numeric_cols) > 1 else (
            columns[1] if len(columns) > 1 else columns[0]
        )

        c1, c2, c3 = st.columns(3)
        with c1:
            x_col = st.selectbox("X-axis", options=columns, index=columns.index(suggested_x), key="x_axis")
        with c2:
            y_col = st.selectbox("Y-axis", options=columns, index=columns.index(suggested_y), key="y_axis")
        with c3:
            chart_options = ["None", "Bar Chart", "Line Chart", "Scatter Plot", "Area Chart", "Histogram", "Pie Chart", "Box Plot"]
            default_chart = chart_suggestion if chart_suggestion in chart_options else "Bar Chart"
            chart_type = st.selectbox(
                "Chart type", options=chart_options, index=chart_options.index(default_chart),
                help=f"AI suggested: {default_chart}", key="chart_type",
            )

        if chart_type != "None":
            chart = create_chart(df, chart_type, x_col, y_col)
            if chart:
                st.plotly_chart(chart, use_container_width=True)

    section_header("Export")
    export_format = st.selectbox("Format", options=["CSV", "Excel", "JSON"], key="export_format")
    export_results(df, export_format)

    if "query_history" not in st.session_state:
        st.session_state.query_history = []
        st.session_state.query_timestamps = []
    st.session_state.query_history.append(query)
    st.session_state.query_timestamps.append(pd.Timestamp.now())


# --- Sidebar: database setup ---
db_type = st.sidebar.selectbox("Database Type", options=["SQLite", "PostgreSQL"])

if db_type == "SQLite":
    uploaded_file = st.sidebar.file_uploader(
        "Upload SQLite Database (optional)", type=["db", "sqlite", "sql"]
    )
    db_file = save_temp_file(uploaded_file) if uploaded_file else str(DEFAULT_SQLITE_DB)

    if Path(db_file).is_file():
        schemas = DB_Config.get_all_schemas(db_file, db_type="sqlite")
        table_names = list(schemas.keys())

        if not table_names:
            st.error("Could not load any tables. Please check the database file.")
        else:
            # All tables are selected automatically; narrow down if you want.
            selected_tables = st.sidebar.multiselect(
                "Tables in use", options=table_names, default=table_names, key="sqlite_tables"
            )

            if selected_tables:
                section_header(f"Selected Tables: {', '.join(selected_tables)}")
                for table in selected_tables:
                    with st.expander(f"Schema: {table}", expanded=False):
                        st.json(schemas[table])

                user_message = st.text_input(
                    "Ask a question about your data", key="user_message",
                    placeholder="e.g. What is the average price per category?",
                )
                if user_message:
                    selected_schemas = {t: schemas[t] for t in selected_tables}
                    with st.spinner("Generating SQL query..."):
                        response = generate_sql_query(user_message, selected_schemas)
                    handle_query_response(response, db_file, db_type="sqlite")
            else:
                st.info("Select at least one table to begin.")
    else:
        st.error(f"Default SQLite database was not found: {DEFAULT_SQLITE_DB}")

elif db_type == "PostgreSQL":
    with st.sidebar.expander("PostgreSQL Connection Details", expanded=True):
        postgres_host = st.text_input("Host", placeholder="PostgreSQL Host")
        postgres_db = st.text_input("DB Name", placeholder="Database Name")
        postgres_user = st.text_input("Username", placeholder="Username")
        postgres_password = st.text_input("Password", type="password", placeholder="Password")

    if all([postgres_host, postgres_db, postgres_user, postgres_password]):
        schemas = DB_Config.get_all_schemas(
            postgres_db, db_type="postgresql", host=postgres_host,
            user=postgres_user, password=postgres_password,
        )
        table_names = list(schemas.keys())

        if not table_names:
            st.info("No tables found in the database.")
        else:
            # All tables are selected automatically; narrow down if you want.
            selected_tables = st.sidebar.multiselect(
                "Tables in use", options=table_names, default=table_names, key="postgresql_tables"
            )

            if selected_tables:
                section_header(f"Selected Tables: {', '.join(selected_tables)}")
                for table in selected_tables:
                    with st.expander(f"Schema: {table}", expanded=False):
                        st.json(schemas[table])

                user_message = st.text_input(
                    "Ask a question about your data", key="user_message_pg",
                    placeholder="e.g. What is the average price per category?",
                )
                if user_message:
                    selected_schemas = {t: schemas[t] for t in selected_tables}
                    with st.spinner("Generating SQL query..."):
                        response = generate_sql_query(user_message, selected_schemas)
                    handle_query_response(
                        response, postgres_db, db_type="postgresql", host=postgres_host,
                        user=postgres_user, password=postgres_password,
                    )
            else:
                st.info("Select at least one table to begin.")
    else:
        st.info("Please fill in all PostgreSQL connection details to start.")

# --- Query history ---
st.markdown("---")
section_header("Query History")
if st.session_state.get("query_history"):
    history_df = pd.DataFrame({
        "Query": st.session_state.query_history,
        "Timestamp": pd.to_datetime(st.session_state.query_timestamps),
    })
    search = st.text_input("Search history", key="history_search")
    if search:
        history_df = history_df[history_df["Query"].str.contains(search, case=False, na=False)]
    st.dataframe(history_df.sort_values("Timestamp", ascending=False), use_container_width=True)
else:
    st.caption("No queries yet. Ask a question above to get started.")
