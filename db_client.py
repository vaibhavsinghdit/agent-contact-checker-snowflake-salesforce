import snowflake.connector

class SnowflakeDB:
    def __init__(self, connection_args: dict):
        self.connection_args = connection_args
        self.connection = None
        self.memo = []

    def start_init_connection(self):
        self.connection = snowflake.connector.connect(**self.connection_args)

    async def execute_query(self, query: str):
        if self.connection is None:
            self.start_init_connection()

        cursor = self.connection.cursor()
        try:
            cursor.execute(query)
            data = [dict(zip([col[0] for col in cursor.description], row)) for row in cursor.fetchall()]
            data_id = f"query_result_{abs(hash(query)) % (10 ** 8)}"
            return data, data_id
        finally:
            cursor.close()

    def add_insight(self, insight: str):
        self.memo.append(insight)

    def get_memo(self):
        return "\n".join(self.memo)
