from fastapi import FastAPI

app = FastAPI(title="Sample managed project")


@app.get("/")
def root() -> dict[str, str]:
    return {"status": "ok"}
