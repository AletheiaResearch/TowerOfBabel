import usd_pipeline


def test_package_imports_and_has_version():
    assert isinstance(usd_pipeline.__version__, str)
    assert usd_pipeline.__version__


def test_eager_exports_resolve():
    # acquisition + viz exports don't require the heavy USD stack
    assert callable(usd_pipeline.acquire_asset)
    assert callable(usd_pipeline.run_acquisition)
    assert callable(usd_pipeline.model_viewer_html)
    assert callable(usd_pipeline.usdz_viewer_html)


def test_lazy_compose_exports_resolve():
    # compose_usd / run_compose / compose_asset are exposed lazily via module __getattr__
    assert callable(usd_pipeline.run_compose)
    assert callable(usd_pipeline.compose_usd)
    assert callable(usd_pipeline.compose_asset)
