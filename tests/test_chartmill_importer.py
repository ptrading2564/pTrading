"""Tests for ChartMill CSV screener importer, earnings filter, and composite ranking."""

from datetime import date, timedelta
import pytest
from app.services.chartmill_importer import ChartMillImporter


@pytest.fixture
def importer() -> ChartMillImporter:
    return ChartMillImporter()


def test_earnings_date_filter(importer: ChartMillImporter):
    today = date(2026, 11, 23)
    tomorrow = today + timedelta(days=1)
    next_week = today + timedelta(days=7)
    past_date = today - timedelta(days=5)

    # Earnings today -> must be filtered
    is_risk, reason = importer.is_earnings_risk("2026-11-23", reference_date=today)
    assert is_risk is True
    assert "Earnings scheduled" in reason

    # Earnings tomorrow -> must be filtered
    is_risk, reason = importer.is_earnings_risk(tomorrow.strftime("%Y-%m-%d"), reference_date=today)
    assert is_risk is True

    # Earnings next week -> safe, not filtered
    is_risk, reason = importer.is_earnings_risk(next_week.strftime("%Y-%m-%d"), reference_date=today)
    assert is_risk is False
    assert reason is None

    # Earnings in the past -> safe
    is_risk, reason = importer.is_earnings_risk(past_date.strftime("%Y-%m-%d"), reference_date=today)
    assert is_risk is False

    # Empty / None earnings -> safe
    is_risk, reason = importer.is_earnings_risk("", reference_date=today)
    assert is_risk is False


def test_composite_ranking_formula(importer: ChartMillImporter):
    # Stock A: High setup rating (9.0), high tech rating (9.0), high RS (85), moderate ATR% (3.0%)
    score_a = importer.calculate_composite_score(
        setup_rating=9.0,
        tech_rating=9.0,
        relative_strength=85.0,
        atr_pct=3.0,
    )
    # 9.0*0.35 (3.15) + 9.0*0.25 (2.25) + 8.5*0.25 (2.125) + 3.0*0.15 (0.45) = 7.975
    assert score_a == 7.975

    # Stock B: Low ratings
    score_b = importer.calculate_composite_score(
        setup_rating=5.0,
        tech_rating=5.0,
        relative_strength=60.0,
        atr_pct=2.0,
    )
    assert score_a > score_b


def test_csv_parser_and_top_38_basket(importer: ChartMillImporter):
    today = date(2026, 11, 23)
    csv_rows = [
        "Ticker,Company Name,Price,ATR %,Setup Rating,Technical Rating,Relative Strength,Next Earnings Date",
        "AAPL,Apple Inc,150.00,2.8%,8,8,80,2026-12-15",
        "NVDA,NVIDIA Corp,140.00,4.2%,9,9,95,2026-12-20",
        "RISK1,Risky Corp Today,25.00,3.5%,9,9,90,2026-11-23",  # Today earnings -> filtered
        "RISK2,Risky Corp Tomorrow,30.00,3.5%,9,9,90,2026-11-24",  # Tomorrow earnings -> filtered
        "TSLA,Tesla Inc,220.00,4.5%,8,7,82,2026-12-05",
    ]
    # Add dummy stocks to test basket capping at 38
    for i in range(1, 45):
        csv_rows.append(f"SYM{i},Dummy Stock {i},{15 + i},3.0%,7,7,75,2026-12-30")

    csv_data = "\n".join(csv_rows)
    result = importer.parse_csv(csv_data, reference_date=today, max_basket_size=38)

    assert result["total_imported"] == 48
    assert result["earnings_filtered_count"] == 2  # RISK1 and RISK2 filtered
    assert result["basket_size"] == 38
    assert len(result["basket_symbols"]) == 38
    assert "NVDA" in result["basket_symbols"]
    assert "AAPL" in result["basket_symbols"]
    assert "RISK1" not in result["basket_symbols"]
    assert "RISK2" not in result["basket_symbols"]
    assert isinstance(result["basket_string"], str)
    assert result["basket_symbols"][0] == "NVDA"  # Highest composite score should be first
