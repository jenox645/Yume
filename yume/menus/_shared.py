"""State shared by the menu modules."""

# BACKEND_INFO from pocket_yume, injected at startup via set_backend_info()
BI: dict = {}


def set_backend_info(bi: dict) -> None:
    """Inject the BACKEND_INFO dict from pocket_yume at startup."""
    global BI
    BI = bi
