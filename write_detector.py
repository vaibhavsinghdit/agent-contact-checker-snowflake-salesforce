import re

class SQLWriteDetector:
    def __init__(self):
        self.write_keywords = [
            "insert", "update", "delete", "merge",
            "create", "alter", "drop", "truncate", "replace"
        ]
        self.sensitive_read_keywords = [
            "count(", "avg(", "sum(", "min(", "max(", "group by", "having"
        ]

    def analyze_query(self, query: str) -> dict:
        lowered = query.lower().strip()

        contains_write = any(
            re.match(rf"^\s*{kw}", lowered) for kw in self.write_keywords  # raw string!
        )

        contains_sensitive_read = any(
            kw in lowered for kw in self.sensitive_read_keywords
        )

        return {
            "contains_write": contains_write,
            "contains_sensitive_read": contains_sensitive_read
        }
