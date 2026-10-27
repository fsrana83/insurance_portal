"""Business rules on vehicle type and usage."""

HEAVY_TYPES = {"BUS", "TRUCK", "TRAILER"}          # buses, trucks / lorries, trailers and heavy equipment
HEAVY_USAGES = {"COMMERCIAL", "GOVERNMENT"}


def usage_error(vehicle_type, usage):
    """Buses, trucks, trailers and heavy equipment can only be commercial or government vehicles."""
    if vehicle_type in HEAVY_TYPES and usage and usage not in HEAVY_USAGES:
        return "Buses, trucks, trailers and heavy equipment can be insured for commercial or government use only."
    return ""


def government_error(usage, customer):
    """A government vehicle belongs to a government department or a government-owned company."""
    if usage == "GOVERNMENT" and customer is not None and not getattr(customer, "government_owned", False):
        return (f"Government usage is for vehicles owned by a government department or government-owned company. "
                f"{customer.name} is not marked as government-owned.")
    return ""
