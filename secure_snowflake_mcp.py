import json
import logging
import mcp.types as types
import yaml

from functools import wraps
from typing import Any, Callable

from db_client import SnowflakeDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("mcp_snowflake_server")

def data_to_yaml(data: Any) -> str:
    return yaml.dump(data, indent=2, sort_keys=False)

def is_column_sensitive(column_name: str, pii_keywords: list[str]) -> bool:
    return any(pii in column_name.lower() for pii in pii_keywords)

def handle_tool_errors(func: Callable) -> Callable:
    @wraps(func)
    async def wrapper(*args, **kwargs) -> list[types.TextContent]:
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {str(e)}")
            return [types.TextContent(type="text", text=f"Error: {str(e)}")]
    return wrapper

async def prefetch_tables(db: SnowflakeDB, credentials: dict, pii_keywords: list[str] = None) -> dict:
    pii_keywords = pii_keywords or ["dob", "phone", "address", "card", "ssn"]

    try:
        logger.info("Prefetching table descriptions")
        table_results, _ = await db.execute_query(
            f"""SELECT table_name, comment 
                FROM {credentials['database']}.information_schema.tables 
                WHERE table_schema = '{credentials['schema'].upper()}'"""
        )
        column_results, _ = await db.execute_query(
            f"""SELECT table_name, column_name, data_type, comment 
                FROM {credentials['database']}.information_schema.columns 
                WHERE table_schema = '{credentials['schema'].upper()}'"""
        )
        tables_brief = {}
        for row in table_results:
            tables_brief[row["TABLE_NAME"]] = {**row, "COLUMNS": {}}
        for row in column_results:
            column_name = row["COLUMN_NAME"]
            row["PII"] = is_column_sensitive(column_name, pii_keywords)
            table = row["TABLE_NAME"]
            del row["TABLE_NAME"]
            tables_brief[table]["COLUMNS"][column_name] = row
        return tables_brief
    except Exception as e:
        logger.error(f"Error prefetching table descriptions: {e}")
        return {}

async def handle_read_query(arguments, db, write_detector, *_, tables_info=None) -> list[types.TextContent]:
    if not arguments or "query" not in arguments:
        raise ValueError("Missing query argument")

    query = arguments["query"]
    logger.info(f"🔍 Incoming read query: {query}")

    analysis = write_detector.analyze_query(query)

    if analysis["contains_write"]:
        raise ValueError("❌ Write operations not allowed in read_query")

    if analysis["contains_sensitive_read"]:
        raise ValueError("❌ Aggregate or statistical queries like COUNT(), AVG(), etc. are restricted.")

    # if "select *" in query.lower():
    #     raise ValueError("❌ SELECT * is not allowed. Please specify safe fields explicitly.")

    if "select *" in query.lower():
        # Try to find the first matching table from tables_info
        for table_name, info in tables_info.items():
            if table_name.lower() in query.lower():
                # Extract non-PII columns
                safe_cols = [
                    col for col, meta in info["COLUMNS"].items()
                    if not meta.get("PII", False)
                ]
                if not safe_cols:
                    raise ValueError("❌ All columns in this table are marked as sensitive.")

                safe_col_list = ", ".join(safe_cols)
                # Replace SELECT * with SELECT safe_col1, safe_col2
                query = query.replace("*", safe_col_list)
                logger.warning(f"✏️ Auto-rewrote SELECT * with safe fields: {safe_col_list}")
                break
        else:
            raise ValueError("❌ SELECT * is not allowed. Unable to identify matching table for safe rewrite.")

    if tables_info:
        for table_name, info in tables_info.items():
            if table_name.lower() in query.lower():
                for col, col_info in info["COLUMNS"].items():
                    if col_info.get("PII", False) and col.lower() in query.lower():
                        raise ValueError(f"❌ Query includes sensitive field: {col}")

    data, data_id = await db.execute_query(query)
    output = {
        "type": "data",
        "data_id": data_id,
        "data": data,
    }
    yaml_output = data_to_yaml(output)
    json_output = json.dumps(output)
    return [
        types.TextContent(type="text", text=yaml_output),
        types.EmbeddedResource(
            type="resource",
            resource=types.TextResourceContents(
                uri=f"data://{data_id}",
                text=json_output,
                mimeType="application/json"
            ),
        ),
    ]
