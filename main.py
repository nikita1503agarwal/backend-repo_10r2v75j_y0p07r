import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from database import create_document, get_documents, db

app = FastAPI(title="Air Quality Analyzer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------------------- AQI CALCULATION (CPCB) --------------------
class IngestReading(BaseModel):
    pm25: float = Field(..., ge=0, description="PM2.5 concentration in µg/m³")
    pm10: float = Field(..., ge=0, description="PM10 concentration in µg/m³")
    co2: Optional[float] = Field(None, ge=0, description="CO₂ concentration in ppm")
    temperature: Optional[float] = Field(None, description="Temperature in °C")
    humidity: Optional[float] = Field(None, ge=0, le=100, description="Relative humidity %")


def _calc_sub_index(Cp: float, breakpoints: List[List[float]]) -> int:
    """Generic sub-index calculation based on CPCB breakpoints.
    Each breakpoint row: [Clow, Chigh, Ilow, Ihigh]
    Returns integer sub-index between 0 and 500.
    """
    Cp = max(0.0, Cp)
    for Cl, Ch, Il, Ih in breakpoints:
        if Cp <= Ch:
            # Linear interpolation
            I = ((Ih - Il) / (Ch - Cl)) * (Cp - Cl) + Il
            return int(round(I))
    return 500  # Cap at Severe


def sub_index_pm25(pm25: float) -> int:
    # CPCB PM2.5 µg/m³ breakpoints
    bps = [
        [0, 30, 0, 50],
        [31, 60, 51, 100],
        [61, 90, 101, 200],
        [91, 120, 201, 300],
        [121, 250, 301, 400],
        [251, 350, 401, 500],
    ]
    return _calc_sub_index(pm25, bps)


def sub_index_pm10(pm10: float) -> int:
    # CPCB PM10 µg/m³ breakpoints
    bps = [
        [0, 50, 0, 50],
        [51, 100, 51, 100],
        [101, 250, 101, 200],
        [251, 350, 201, 300],
        [351, 430, 301, 400],
        [431, 600, 401, 500],
    ]
    return _calc_sub_index(pm10, bps)


def aqi_category(aqi: int) -> str:
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Satisfactory"
    if aqi <= 200:
        return "Moderate"
    if aqi <= 300:
        return "Poor"
    if aqi <= 400:
        return "Very Poor"
    return "Severe"


# -------------------- BASIC ROUTES --------------------
@app.get("/")
def read_root():
    return {"message": "Air Quality Analyzer API is running"}


@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": [],
    }

    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, "name") else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    # Environment variables
    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"

    return response


# -------------------- AIR QUALITY ENDPOINTS --------------------
COLLECTION = "airqualityreading"


def _serialize(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in doc.items() if k != "_id"}
    # Ensure datetimes are ISO strings
    for k, v in list(out.items()):
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
    return out


@app.post("/api/air/ingest")
def ingest_reading(payload: IngestReading):
    # Compute AQI sub-indices
    si_pm25 = sub_index_pm25(payload.pm25)
    si_pm10 = sub_index_pm10(payload.pm10)

    aqi = max(si_pm25, si_pm10)
    category = aqi_category(aqi)

    data = payload.model_dump()
    data.update({
        "aqi": aqi,
        "category": category,
        "timestamp": datetime.now(timezone.utc),
    })

    try:
        create_document(COLLECTION, data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:120]}")

    return {"status": "ok", "aqi": aqi, "category": category}


@app.get("/api/air/latest")
def get_latest():
    try:
        docs = get_documents(COLLECTION, {}, limit=None)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:120]}")

    if not docs:
        # Provide a friendly default if no data yet
        return {
            "pm25": 0,
            "pm10": 0,
            "co2": None,
            "temperature": None,
            "humidity": None,
            "aqi": 0,
            "category": "Good",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # Find the latest by created_at or timestamp
    latest = sorted(
        docs,
        key=lambda d: d.get("timestamp") or d.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[0]

    return _serialize(latest)


@app.get("/api/air/history")
def get_history(limit: int = 50):
    lim = max(1, min(limit, 500))
    try:
        docs = get_documents(COLLECTION, {}, limit=None)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:120]}")

    # Sort and trim
    docs_sorted = sorted(
        docs,
        key=lambda d: d.get("timestamp") or d.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:lim]

    # Return in chronological order for charts
    docs_sorted.reverse()
    return [_serialize(d) for d in docs_sorted]


# Backward-compat alias mentioned in the brief
@app.get("/get_data")
def get_data_alias():
    return get_latest()


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
