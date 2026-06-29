import usd_pipeline


def test_package_imports_and_has_version():
    assert isinstance(usd_pipeline.__version__, str)
    assert usd_pipeline.__version__


def test_lazy_compose_exports_resolve():
    # compose_usd / run_compose are exposed lazily via module __getattr__
    assert callable(usd_pipeline.run_compose)
    assert callable(usd_pipeline.compose_usd)
