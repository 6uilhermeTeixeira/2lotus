"""Read chart-ready candles from the immutable dataset published by 1Lotus."""

from datetime import date, datetime, timedelta, timezone

import duckdb

from .local_dataset import DatasetError, DatasetRelease
from .schemas import MarketCandlePoint


SUPPORTED_RESOLUTIONS = {"1m", "5m", "1h", "1d"}


def load_chart_candles(
    release: DatasetRelease, symbol: str, resolution: str, days: int, *, limit: int = 50_000
) -> list[MarketCandlePoint]:
    if resolution not in SUPPORTED_RESOLUTIONS:
        raise DatasetError(f"resolução não suportada: {resolution}")
    if str(release.manifest.get("instrument", "")).upper() != symbol:
        raise DatasetError(f"dataset ativo não corresponde a {symbol}")
    coverage_end = release.manifest.get("coverage_end")
    if not isinstance(coverage_end, str):
        raise DatasetError("dataset não informa o fim da cobertura")
    cutoff = date.fromisoformat(coverage_end) - timedelta(days=days - 1)
    cutoff_ms = int(datetime.combine(cutoff, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
    aggregations = release.manifest.get("aggregations", {})
    rows = aggregations.get(resolution, []) if isinstance(aggregations, dict) else []
    paths: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            continue
        partition_day = row.get("day")
        if not isinstance(partition_day, str) or date.fromisoformat(partition_day) < cutoff:
            continue
        path = (release.root / row["path"]).resolve()
        if not path.is_relative_to(release.root) or not path.is_file():
            raise DatasetError(f"agregação ausente ou fora da raiz: {row['path']}")
        paths.append(str(path))
    if not paths:
        return []
    connection = duckdb.connect(":memory:")
    try:
        result = connection.execute(
            """SELECT * FROM (
                   SELECT t,o,h,l,c,coalesce(bid_volume,0)+coalesce(ask_volume,0) AS volume
                   FROM read_parquet(?) WHERE t >= ? ORDER BY t DESC LIMIT ?
               ) ordered_rows ORDER BY t""",
            [paths, cutoff_ms, limit],
        ).fetchall()
    finally:
        connection.close()
    return [
        MarketCandlePoint(t=t, o=o, h=h, l=low_price, c=c, volume=volume)
        for t, o, h, low_price, c, volume in result
    ]
