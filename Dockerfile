FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml constraints.txt ./
COPY app app
RUN pip install --no-cache-dir -c constraints.txt .
COPY alembic.ini .
COPY migrations migrations
RUN useradd --uid 10001 --create-home growth
USER growth
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
