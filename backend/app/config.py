"""Environment configuration. No secrets manager in this POC - plain env vars."""
import os

from sqlalchemy.engine import URL


class Settings:
    # --- Google Cloud ---
    project_id: str = os.getenv("GCP_PROJECT", "")

    # --- Cloud Storage ---
    bucket: str = os.getenv("GCS_BUCKET", "")

    # --- Document AI ---
    docai_location: str = os.getenv("DOCAI_LOCATION", "eu")
    docai_processor_id: str = os.getenv("DOCAI_PROCESSOR_ID", "")

    # --- Vertex AI ---
    # Model availability is region specific; the global endpoint works
    # everywhere, which is why this is its own setting.
    vertex_location: str = os.getenv("VERTEX_LOCATION", "global")
    vertex_model: str = os.getenv("VERTEX_MODEL", "gemini-2.5-flash")

    # --- Cloud SQL ---
    # Either INSTANCE_CONNECTION_NAME (Cloud Run unix socket) or DATABASE_URL.
    instance_connection_name: str = os.getenv("INSTANCE_CONNECTION_NAME", "")
    db_user: str = os.getenv("DB_USER", "postgres")
    db_password: str = os.getenv("DB_PASSWORD", "")
    db_name: str = os.getenv("DB_NAME", "quotepoc")
    database_url: str = os.getenv("DATABASE_URL", "")

    engine_version: str = "1.0.0"

    def sqlalchemy_url(self):
        """Connection URL.

        Built with URL.create rather than string formatting so a password
        containing @ / : or ? cannot break the URL.
        """
        if self.database_url:
            return self.database_url
        if self.instance_connection_name:
            socket = f"/cloudsql/{self.instance_connection_name}/.s.PGSQL.5432"
            return URL.create(
                drivername="postgresql+pg8000",
                username=self.db_user,
                password=self.db_password,
                database=self.db_name,
                query={"unix_sock": socket},
            )
        return "sqlite+pysqlite:///./quotepoc.db"


settings = Settings()
