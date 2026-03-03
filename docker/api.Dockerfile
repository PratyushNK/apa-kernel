FROM python:3.10

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install uv
RUN uv sync --extra api --frozen

COPY . .

CMD ["uv", "run", "uvicorn", "apps.main:app", "--host", "0.0.0.0", "--port", "8000"]



# docker build -f docker/api.Dockerfile -t apa-api .