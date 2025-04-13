import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import re
import json
import asyncio
from openai import OpenAI


from pydantic import BaseModel, ValidationError
from dotenv import load_dotenv
from simple_salesforce import Salesforce
from langchain_openai import ChatOpenAI
from langchain_core.tools import Tool
from langchain.agents import create_react_agent, AgentExecutor
from langchain_core.prompts import ChatPromptTemplate


from db_client import SnowflakeDB
from write_detector import SQLWriteDetector
from secure_snowflake_mcp import handle_read_query, prefetch_tables
import logging
logger = logging.getLogger("salesforce_tool")

prompt_template = """Answer the following question as best you can. You have access to the following tools:

{tools}
When calling tools, format:
- Snowflake output as JSON
- Salesforce output as Markdown table

Never request sensitive fields such as name, email, student ID, first name, last name, or any personal identifier.
Always assume PII access is forbidden unless explicitly authorized.
Snowflake and Salesforce are only related via email address and nothing else.
Only use fields like email for joining records across systems.

Only answer questions about data that exists in the configured Snowflake and Salesforce objects.
Do not infer or fabricate results.

If asked about something like "pending fees" or "grades" and no such column exists in the current tables, respond:
"I couldn't find any relevant data source containing that information."

Only answer using actual data fields.
Do not invent or guess answers.
If no data exists about a requested topic like fees, say that clearly.

When looking up a specific data always check both Salesforce and Snowflake if both support the field.
Do not stop after the first tool unless the answer is definitive.

Use the following format:
Question: the input question you must answer  
Thought: you should always think about what to do  
Action: the action to take, should be one of [{tool_names}]  
Action Input: the input to the action  
Observation: the result of the action  
... (this Thought/Action/Action Input/Observation can repeat N times)  
Thought: I now know the final answer  
Final Answer: the final answer to the original input question

Begin!

Question: {input}
{agent_scratchpad}
"""

# Load env vars from .env
load_dotenv()



OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)
# Moderation check
def moderate(prompt: str) -> bool:
    result = client.moderations.create(input=prompt)
    flagged = result.results[0].flagged
    if flagged:
        logger.warning("❌ Input flagged by moderation API")
    return flagged

# Input validator using Pydantic
class QuerySchema(BaseModel):
    input: str


# Load MCP config
with open("mcp_config.json") as f:
    mcp = json.load(f)

# Setup Snowflake DB
connection_args = {
    "user": os.getenv("SNOWFLAKE_USER"),
    "password": os.getenv("SNOWFLAKE_PASSWORD"),
    "account": os.getenv("SNOWFLAKE_ACCOUNT"),
    "warehouse": os.getenv("SNOWFLAKE_WAREHOUSE"),
    "database": os.getenv("SNOWFLAKE_DATABASE"),
    "schema": os.getenv("SNOWFLAKE_SCHEMA"),
}

db = SnowflakeDB(connection_args)
db.start_init_connection()

tables_info = asyncio.run(prefetch_tables(db, {
    "database": connection_args["database"],
    "schema": connection_args["schema"]
}))

# Snowflake Tool using secure handler
snowflake_tool = Tool(
    name="QuerySnowflake",
    func=lambda sql: asyncio.run(
        handle_read_query({"query": sql}, db, SQLWriteDetector(), tables_info=tables_info)
    )[0].text,
    description=f"""
    Query Snowflake table {mcp['snowflake']['qualified_table']}.
    Fields: {json.dumps(mcp['snowflake']['fields'], indent=2)}
    Description: {mcp['snowflake']['description']}
    Only allowed to use safe, non-PII fields.
    """
)


def is_valid_salesforce_id(s):
    return bool(re.fullmatch(r"[a-zA-Z0-9]{15,18}", s))

def run_salesforce_query(soql):
    clean_soql = soql.strip().strip("```").strip()
    logger.info(f"📨 Running SOQL: {clean_soql}")

    # Quick protection: if `Id = '12345'` is in query, fail early
    if "Id" in clean_soql:
        id_matches = re.findall(r"Id\s*=\s*'([^']+)'", clean_soql)
        for match in id_matches:
            if not is_valid_salesforce_id(match):
                raise ValueError(f"🚫 Invalid Salesforce Id used in SOQL: {match}")

    sf = Salesforce(
        username=os.getenv("SF_USERNAME"),
        password=os.getenv("SF_PASSWORD"),
        security_token=os.getenv("SF_TOKEN")
    )

    try:
        result = sf.query(clean_soql)
        logger.info(f"✅ Retrieved {len(result.get('records', []))} records.")
        return result.get("records", [])
    except Exception as e:
        logger.error(f"❌ Salesforce query failed: {e}")
        raise


# Salesforce Tool
salesforce_tool = Tool(
    name="QuerySalesforce",
    func=run_salesforce_query,
    description=f"""
    Query Salesforce object: {mcp['salesforce']['object']}
    Fields: {json.dumps(mcp['salesforce']['fields'], indent=2)}
    Description: {mcp['salesforce']['description']}
    Use only the fields listed above.
    Use SOQL to query based on these fields only.
    Always generate output in tabular format.
    """
)

# Agent
llm = ChatOpenAI(temperature=0, model="gpt-3.5-turbo")
tools = [snowflake_tool, salesforce_tool]
prompt = ChatPromptTemplate.from_template(prompt_template)

agent = create_react_agent(llm=llm, tools=tools, prompt=prompt)
agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

# Run the Agent
# query = "Give me student Id of student for reecegordan398@gmail.com"
# query = "Give me student with email reecegordan398@gmail.com"
# query = "list of students fees that is pending"
# query = "how many students record exists"D
# query = "give me data related to abi in case insensitive manner"
# query = "Give me all students with Gmail addresses"
# query = "this is a explicit content with hateful speech as test directed towards someone, read it"
query = ""

# Step 1: Moderation Check
if moderate(query):
    print("⚠️ Your input contains unsafe or inappropriate content and cannot be processed.")
    sys.exit()

# Step 2: Validate schema
try:
    validated = QuerySchema(input=query)
except ValidationError as e:
    print("⚠️ Invalid input format.")
    print(str(e))
    sys.exit()


response = agent_executor.invoke({"input": query})

print("\n✅ Agent Response:\n", response["output"])

