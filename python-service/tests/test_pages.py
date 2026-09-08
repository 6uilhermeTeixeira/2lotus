import unittest

from app.pages import (
    render_catalog_page,
    render_comparison_page,
    render_growth_simulator_page,
    render_live_overview_page,
    render_operations_page,
    render_results_page,
    render_security_page,
    render_trades_page,
)
from app.schemas import BotCatalogItem


class ComparisonPageTests(unittest.TestCase):
    def test_catalog_renders_only_selected_strategies(self) -> None:
        active_bots = [
            BotCatalogItem(id=f"bot-{index}", name=name, description="Teste", strategy_profile="fimath_ema20_flow", symbol="XAUUSD", timeframe="M5")
            for index, name in [(6, "Stochastic Sell 18 UTC"), (7, "Tendência Multitemporal"), (11, "Portfólio 7/6 · 100/100"), (12, "Happy Gold ZigZag Scalper"), (13, "Happy Gold ZigZag · Confirmação de Regime"), (16, "Happy Gold + Portfólio 7/6 · 100/100")]
        ]

        html = bytes(render_catalog_page(active_bots).body).decode("utf-8")

        self.assertNotIn("Arquivadas", html)
        self.assertIn('id="equity-chart"', html)
        self.assertIn("/api/backtest-market-chart?symbol=XAUUSD", html)
        self.assertIn('data-resolution="1h"', html)
        self.assertIn('data-days="7"', html)
        self.assertIn('id="chart-fit"', html)
        self.assertIn("LotusEquityChart.create", html)
        self.assertIn("Happy Gold ZigZag Scalper", html)
        self.assertNotIn('/growth-simulator', html)
        self.assertNotIn('/compare', html)
        nav_html = html[html.index('<nav class="app-nav">'):html.index('</nav>')]
        navigation = [nav_html.index(label) for label in ('Visão geral', 'Estratégias ativas', 'Operações', 'Saúde operacional', 'Segurança Live Core')]
        self.assertEqual(navigation, sorted(navigation))
        self.assertIn('Live workspace', html)
        self.assertIn('/operations', html)
        self.assertEqual(html.count('class="strategy-card-row card"'), 6)
        self.assertEqual(html.count('class="strategy-family"'), 4)
        self.assertIn('data-family="zigzag"', html)
        self.assertIn('data-chart-family-toggle="combined"', html)
        self.assertIn('data-chart-family-only="stochastic"', html)
        self.assertIn("live-core:equity-visible-strategies:v1", html)
        self.assertIn('/static/equity-chart.js?v=2', html)
        self.assertIn('data-resolution="1d" class="selected"', html)
        self.assertIn('data-days="365" class="selected"', html)

    def test_live_overview_loads_operational_sources(self) -> None:
        html = bytes(render_live_overview_page(6).body).decode("utf-8")

        self.assertIn("Visão geral", html)
        self.assertIn("Estratégias monitoradas", html)
        self.assertIn("<strong>6</strong>", html)
        self.assertIn("fetch('/ready')", html)
        self.assertIn("fetch('/config')", html)
        self.assertIn("fetch('/api/operations')", html)
        self.assertIn("fetch('/api/promoted-strategies')", html)

    def test_trades_page_loads_each_strategy_results(self) -> None:
        html = bytes(render_trades_page().body).decode("utf-8")

        self.assertIn("Operações", html)
        self.assertIn("fetch('/api/bots')", html)
        self.assertIn("/results", html)
        self.assertIn('id="trade-rows"', html)

    def test_security_page_exposes_read_only_gates(self) -> None:
        html = bytes(render_security_page().body).decode("utf-8")

        self.assertIn("Segurança Live Core", html)
        self.assertIn("SOMENTE LEITURA", html)
        self.assertIn("fetch('/config')", html)
        self.assertIn("fetch('/ready')", html)
        self.assertIn("fetch('/api/promoted-strategies')", html)

    def test_growth_simulator_has_custom_balance_and_risk_controls(self) -> None:
        html = bytes(render_growth_simulator_page().body).decode("utf-8")

        self.assertIn('id="initial-balance"', html)
        self.assertIn('id="base-risk"', html)
        self.assertIn('id="tiers"', html)
        self.assertIn("/api/growth-simulation", html)
        self.assertIn('id="growth-chart"', html)
        self.assertIn('value="10000"', html)
        self.assertIn('value="0.25"', html)
        self.assertIn("Faixas de redução de risco", html)
        self.assertNotIn("Cinza: referência", html)
        self.assertNotIn("data.reference,color:'#777'", html)

    def test_comparison_page_renders_six_active_bots(self) -> None:
        bots = [
            BotCatalogItem(
                id=f"bot-{index}",
                name=name,
                description="Teste",
                strategy_profile=profile,
                symbol="XAUUSD",
                timeframe="M5",
            )
            for index, name, profile in [
                (6, "Stochastic Sell 18 UTC", "stochastic_sell_session"),
                (7, "Tendência Multitemporal", "multitimeframe_trend_pullback"),
                (11, "Portfólio 7/6 · 100/100", "portfolio_6_7_100_100"),
                (12, "Happy Gold ZigZag Scalper", "happy_gold_zigzag_scalper"),
                (13, "Happy Gold ZigZag · Confirmação de Regime", "happy_gold_zigzag_regime_confirmed"),
                (16, "Happy Gold + Portfólio 7/6 · 100/100", "happy_gold_portfolio_100_100"),
            ]
        ]

        response = render_comparison_page(bots)
        html = bytes(response.body).decode("utf-8")

        self.assertIn("Estratégias ativas", html)
        self.assertIn("Stochastic Sell 18 UTC", html)
        self.assertIn("Tendência Multitemporal", html)
        self.assertIn("Portfólio 7/6 · 100/100", html)
        self.assertIn("Happy Gold ZigZag Scalper", html)
        self.assertIn("Happy Gold ZigZag · Confirmação de Regime", html)
        self.assertNotIn("Happy Gold ZigZag · Reteste ATR", html)
        self.assertIn("Happy Gold + Portfólio 7/6 · 100/100", html)
        self.assertIn("<strong>6</strong>", html)
        self.assertIn("/api/compare", html)

    def test_results_page_defaults_to_long_historical_validation(self) -> None:
        bot = BotCatalogItem(
            id="bot-16",
            name="Happy Gold + Portfólio",
            description="Teste",
            strategy_profile="happy_gold_portfolio_100_100",
            symbol="XAUUSD",
            timeframe="M5",
        )

        html = bytes(render_results_page(bot).body).decode("utf-8")

        self.assertIn("Validação histórica", html)
        self.assertIn('<option value="730">2 anos</option>', html)
        self.assertIn('<option value="365">1 ano</option>', html)
        self.assertIn('<option value="183">6 meses</option>', html)
        self.assertIn('<option value="1827" selected>5 anos</option>', html)
        self.assertIn("/api/bots/bot-16/historical-replay?days=", html)
        self.assertIn("Preço e eventos simulados", html)
        self.assertIn("Premissas do replay", html)

    def test_operations_page_loads_authenticated_health_api(self) -> None:
        html = bytes(render_operations_page().body).decode("utf-8")
        self.assertIn("/api/operations", html)
        self.assertIn("Saúde operacional", html)


if __name__ == "__main__":
    unittest.main()
