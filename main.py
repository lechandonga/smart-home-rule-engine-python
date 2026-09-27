import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.deps import init_services
from app.errors import DomainError
from app.routers import devices, rules

app = FastAPI(title="Smart Home Rule Engine")


@app.on_event("startup")
def _startup() -> None:
    init_services()


@app.exception_handler(DomainError)
def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message,
                           "detail": exc.detail}},
    )


app.include_router(devices.router, prefix="/devices")
app.include_router(rules.router, prefix="/rules")


@app.get("/")
def read_root():
    return {"service": "smart-home-rule-engine", "status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
