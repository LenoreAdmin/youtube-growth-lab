from sqlalchemy.schema import CreateTable, CreateIndex
from sqlalchemy.dialects import postgresql
from app.models import Base

def test_all_tables_and_indexes_compile_for_postgresql():
    for table in Base.metadata.sorted_tables:
        sql=str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "CREATE TABLE" in sql
        for index in table.indexes:
            assert "CREATE" in str(CreateIndex(index).compile(dialect=postgresql.dialect()))
