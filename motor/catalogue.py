"""Vehicle make / model catalogue (starter list; users add more with the + button)."""
DEFAULT = {
    "Toyota": ["Corolla", "Camry", "Yaris", "Land Cruiser", "Prado", "Hilux", "Fortuner", "RAV4", "Hiace", "Innova"],
    "Nissan": ["Sunny", "Altima", "Patrol", "X-Trail", "Kicks", "Navara", "Urvan"],
    "Hyundai": ["Accent", "Elantra", "Sonata", "Tucson", "Santa Fe", "Creta", "H1"],
    "Kia": ["Picanto", "Rio", "Cerato", "Sportage", "Sorento", "Carnival"],
    "Mitsubishi": ["Lancer", "Pajero", "L200", "Outlander", "ASX"],
    "Honda": ["Civic", "Accord", "CR-V", "City"],
    "Ford": ["Explorer", "Edge", "F-150", "Ranger", "Mustang"],
    "Chevrolet": ["Tahoe", "Suburban", "Silverado", "Captiva"],
    "Isuzu": ["D-Max", "NPR", "NQR", "FVR"],
    "Mercedes": ["C-Class", "E-Class", "S-Class", "GLE", "G-Class"],
    "BMW": ["3 Series", "5 Series", "X5", "X3"],
    "Suzuki": ["Swift", "Dzire", "Vitara", "Ertiga"],
    "Mazda": ["3", "6", "CX-5", "CX-9"],
    "Lexus": ["LX 570", "RX", "ES"],
    "Land Rover": ["Defender", "Range Rover", "Discovery"],
    "Mack": ["Granite", "Anthem"], "Hino": ["300", "500"], "Tata": ["Ace", "Prima"], "Yamaha": ["YZF", "NMAX"],
}


def seed(models=None):
    """Idempotent: starter list plus every make / model already on a vehicle."""
    from .models import Vehicle, VehicleMake, VehicleModel
    pairs = [(mk, md) for mk, mds in DEFAULT.items() for md in mds]
    pairs += list(Vehicle.objects.values_list("make", "model").distinct())
    for mk, md in pairs:
        if not mk or not md:
            continue
        make, _ = VehicleMake.objects.get_or_create(name=mk.strip()[:40])
        VehicleModel.objects.get_or_create(make=make, name=md.strip()[:40])


def as_dict():
    from .models import VehicleModel
    out = {}
    for m in VehicleModel.objects.select_related("make"):
        out.setdefault(m.make.name, []).append(m.name)
    from .models import VehicleMake
    for mk in VehicleMake.objects.values_list("name", flat=True):
        out.setdefault(mk, [])
    return out
