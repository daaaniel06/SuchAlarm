"""Category presets and provider configuration."""

from typing import Any

SOURCES = {'ebay': 'eBay', 'kleinanzeigen': 'Kleinanzeigen', 'vinted': 'Vinted', 'idealo': 'idealo · Shops'}
HOSTS = {
    'ebay': {'www.ebay.de', 'ebay.de'},
    'kleinanzeigen': {'www.kleinanzeigen.de', 'kleinanzeigen.de'},
    'vinted': {'www.vinted.de', 'vinted.de'},
    'idealo': {'www.idealo.de', 'idealo.de'},
}
# Only these providers can search around a postcode; pickup searches use them alone.
PICKUP_SOURCES = ('ebay', 'kleinanzeigen')
RADII = (10, 25, 50, 100, 200)
PLATFORMS = {
    'ps5': 'PS5',
    'ps4': 'PS4',
    'switch2': 'Switch 2',
    'switch': 'Switch',
    'xbox-series': 'Xbox Series',
    'xbox-one': 'Xbox One',
    'pc': 'PC',
}
# Vinted files games under one section and filters the platform separately (video_game_platform_ids[]).
# IDs checked live on 29.10.2026; PS5 games are also tagged "PS5 Pro".
VINTED_PLATFORMS: dict[str, tuple[str, ...]] = {
    'ps5': ('1281', '8582'),
    'ps4': ('1280',),
    'switch2': ('6478',),
    'switch': ('1273',),
    'xbox-series': ('1291',),
    'xbox-one': ('1290',),
    'pc': ('1276',),
}
# eBay's item specific "Plattform" in the video game section, as its own filter links name it (checked live on
# 29.10.2026). Sellers set it themselves, so other platforms slip in; Switch 2 has no value yet.
EBAY_PLATFORMS: dict[str, tuple[str, ...]] = {
    'ps5': ('Sony PlayStation 5',),
    'ps4': ('Sony PlayStation 4',),
    'switch': ('Nintendo Switch',),
    'xbox-series': ('Microsoft Xbox Series X|S',),
    'xbox-one': ('Microsoft Xbox One',),
    'pc': ('PC',),
}
PROVIDER_PLATFORMS = {'vinted': VINTED_PLATFORMS, 'ebay': EBAY_PLATFORMS}
# Car and bike filters. Kleinanzeigen filters them itself (attributes after "k0c…", checked live on 01.11.2026);
# its cards show neither, so they are never checked again locally.
FUELS = {'benzin': 'Benzin', 'diesel': 'Diesel', 'elektro': 'Elektro', 'hybrid': 'Hybrid'}
GEARBOXES = {'automatik': 'Automatik', 'manuell': 'Schaltgetriebe'}
BIKE_TYPES = {
    'ebike': 'E-Bike',
    'mountainbike': 'Mountainbike',
    'rennrad': 'Rennrad',
    'trekking': 'Trekking & Cross',
    'city': 'Cityrad',
    'kinder': 'Kinderrad',
}
KLEINANZEIGEN_BIKES = {
    'ebike': 'fahrraeder.type_s:ebike',
    'mountainbike': 'fahrraeder.type_s:mountainbike',
    'rennrad': 'fahrraeder.type_s:rennrad',
    'trekking': 'fahrraeder.type_s:cross',
    'city': 'fahrraeder.type_s:city',
    'kinder': 'fahrraeder.art_s:kinder',
}
# eBay files E-Bikes in a section of their own beside "Fahrräder" (01.11.2026: "e-bike" 301 hits there, 6.500+ here).
EBAY_EBIKES = '74469'
# Car titles often leave the make out ("Golf 7 TSI") inside the provider's car section; the make may be missing.
CAR_MAKES = frozenset(
    [
        'vw',
        'audi',
        'bmw',
        'mercedes',
        'opel',
        'ford',
        'skoda',
        'seat',
        'cupra',
        'renault',
        'peugeot',
        'citroen',
        'fiat',
        'toyota',
        'hyundai',
        'kia',
        'mazda',
        'nissan',
        'honda',
        'volvo',
        'porsche',
        'mini',
        'dacia',
        'tesla',
        'smart',
        'suzuki',
        'mitsubishi',
        'jeep',
        'alfa',
        'romeo',
        'lancia',
        'jaguar',
        'land',
        'rover',
        'subaru',
        'chevrolet',
        'chrysler',
        'dodge',
        'lexus',
        'saab',
        'ds',
        'mg',
        'byd',
        'polestar',
    ]  # one line of makes reads better than one make per line
)
# A category presets a new search and narrows the provider search to its own section.
# Section IDs were checked live on 23.10.2026 (eBay _sacat, Kleinanzeigen c…, Vinted catalog[]).
CATEGORIES: dict[str, dict[str, Any]] = {
    'videospiele': dict(
        label='Videospiele',
        hint='PlayStation, Switch, Xbox',
        delivery='versand',
        sources=['ebay', 'kleinanzeigen', 'vinted', 'idealo'],
        # Decoration is sold under a game's name and filed as the game itself (Vinted, 29.10.2026).
        excluded='Hülle, Konsolenhülle, Spielehülle, nur Cover, Code, Downloadcode, Download, Poster, Plakat, Affiche, Mauspad, Mousepad, Tapis de souris, '
        'Schild, Targa, Bilderrahmen, Cadre, Flagge, Drapeau, Figur, Figurine, Sticker',
        negotiable=False,
        free=False,
        radius=0,
        filters=['platform'],
        portal={'ebay': '139973', 'kleinanzeigen': '227', 'vinted': '3026'},
    ),
    'elektronik': dict(
        label='Elektronik',
        hint='Handy, Laptop, Kopfhörer',
        delivery='versand',
        sources=['ebay', 'kleinanzeigen', 'vinted', 'idealo'],
        excluded='defekt, Bastler',
        negotiable=False,
        free=False,
        radius=0,
        filters=[],
        portal={'kleinanzeigen': '161', 'vinted': '2994'},
    ),
    'moebel': dict(
        label='Möbel',
        hint='Abholung in deiner Nähe',
        delivery='abholung',
        sources=['ebay', 'kleinanzeigen'],
        excluded='',
        negotiable=True,
        free=True,
        radius=25,
        filters=[],
        portal={'ebay': '3197', 'kleinanzeigen': '80'},
    ),
    'fahrraeder': dict(
        label='Fahrräder',
        hint='Abholung in deiner Nähe',
        delivery='abholung',
        sources=['ebay', 'kleinanzeigen'],
        excluded='defekt',
        negotiable=True,
        free=True,
        radius=50,
        filters=['bike_type'],
        portal={'ebay': '177831', 'kleinanzeigen': '217'},
    ),
    'autos': dict(
        label='Autos',
        hint='Erstzulassung, Kilometer',
        delivery='abholung',
        sources=['kleinanzeigen'],
        excluded='Unfall, Motorschaden',
        negotiable=True,
        free=False,
        radius=100,
        filters=['year_from', 'km_to', 'fuel', 'gearbox'],
        portal={'ebay': '9801', 'kleinanzeigen': '216'},
    ),
    'sonstiges': dict(
        label='Sonstiges',
        hint='Ohne Vorgaben',
        delivery='versand',
        sources=['ebay', 'kleinanzeigen', 'vinted', 'idealo'],
        excluded='',
        negotiable=False,
        free=False,
        radius=0,
        filters=[],
        portal={},
    ),
}
