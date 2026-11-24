"""Benchmark data used when the account has no history yet, for every product category.

Numbers are approximate Meta e-commerce (conversion objective) medians.
Once the Meta account is connected, backend/history.py overrides these with
the account's own 90-day / 2-year figures, which are far more accurate.

cpm_usd: cost per 1000 impressions
ctr: link click-through rate (0-1)
cvr: purchase conversion rate on landing page (0-1)
"""

COUNTRY_BENCHMARKS: dict[str, dict] = {
    "US": {"name": "United States", "cpm_usd": 14.0, "ctr": 0.012, "cvr": 0.020, "min_daily_usd": 5},
    "GB": {"name": "United Kingdom", "cpm_usd": 10.0, "ctr": 0.012, "cvr": 0.020, "min_daily_usd": 5},
    "CA": {"name": "Canada", "cpm_usd": 11.0, "ctr": 0.012, "cvr": 0.019, "min_daily_usd": 5},
    "AU": {"name": "Australia", "cpm_usd": 11.5, "ctr": 0.012, "cvr": 0.019, "min_daily_usd": 5},
    "DE": {"name": "Germany", "cpm_usd": 8.5, "ctr": 0.010, "cvr": 0.017, "min_daily_usd": 5},
    "FR": {"name": "France", "cpm_usd": 7.5, "ctr": 0.010, "cvr": 0.016, "min_daily_usd": 5},
    "AE": {"name": "UAE", "cpm_usd": 6.5, "ctr": 0.013, "cvr": 0.018, "min_daily_usd": 5},
    "SA": {"name": "Saudi Arabia", "cpm_usd": 5.5, "ctr": 0.013, "cvr": 0.017, "min_daily_usd": 5},
    "PK": {"name": "Pakistan", "cpm_usd": 0.9, "ctr": 0.015, "cvr": 0.012, "min_daily_usd": 1},
    "IN": {"name": "India", "cpm_usd": 1.2, "ctr": 0.014, "cvr": 0.011, "min_daily_usd": 1},
}

# Product categories. Each has multipliers on (cpm, ctr, cvr) relative to generic fashion
# e-commerce, the price its benchmarks assume, a demand season (backend/seasonality.py), the sales
# events that matter to its buyers, and guidance for the AI planner and the fixed template.
#   season: winter | summer | gifting | steady
#   tags:   which events lift it: gift (Valentine, Christmas), women (Mother's Day),
#           men (Father's Day), eid (Eid shopping), home (Eid al-Adha hosting)
def _cat(label, group, cpm, ctr, cvr, typical_price, season, tags, noun, audience, hook, risk, guidance,
         studio, lifestyle, image_note=""):
    return {"label": label, "group": group, "cpm": cpm, "ctr": ctr, "cvr": cvr, "typical_price": typical_price,
            "season": season, "tags": set(tags), "noun": noun, "audience": audience, "hook": hook, "risk": risk,
            "guidance": guidance, "studio": studio, "lifestyle": lifestyle, "image_note": image_note}


CATEGORIES: dict[str, dict] = {
    "leather_jackets": _cat(
        "Leather jackets", "Fashion", 1.00, 1.00, 0.90, 150, "winter", {"gift", "men"}, "jacket",
        "22-45, interests: leather jackets, motorcycle apparel, streetwear, AllSaints / Schott / Zara shoppers",
        "Real leather, cut to fit: the {product}",
        "Leather niche: emphasise material quality, fit guide and returns; peak demand Sept-Jan, expect higher CPMs in Q4.",
        "Generic leather / biker / bomber / aviator jackets and upperwear. Buyers shop on style, material quality and "
        "price. Target: 22-45, interests like leather jackets, motorcycle apparel, streetwear, Zara/AllSaints/Schott "
        "shoppers; broad Advantage+ works well. Creatives: carousels of angles (front, back, on a model, stitching and "
        "leather grain close-ups), size/fit confidence, free shipping. Peak season Sept-Jan in northern countries.",
        "Studio product shot on an invisible mannequin, dark grey seamless background, soft key light, visible leather grain and stitching",
        "Model wearing it on an evening city street, warm neon reflections, shallow depth of field, confident stance"),
    "celebrity_jackets": _cat(
        "Celebrity / movie-inspired jackets", "Fashion", 1.05, 1.25, 0.85, 150, "winter", {"gift", "men"}, "jacket",
        "18-40, fans of the film/series/artist behind the product, cosplay, comic-con, pop-culture pages",
        "The on-screen biker look you have been searching for, now shipping to {country}",
        "Celebrity/movie niche: do not use official stills, logos or the actor's face; Meta rejects trademark/likeness use. "
        "Use 'inspired by' wording and own photos.",
        "Jackets inspired by celebrities, films, TV series and games (e.g. a jacket seen on a character or star). Buyers "
        "are fans: target interests in the specific film/series/artist, cosplay, comic-cons, fan pages (targeting may "
        "use those interests, the ad copy may not name them). Hooks describe the look, e.g. 'the on-screen biker "
        "look'. Higher CTR but more impulse browsing, so lower CVR. Risk: Meta may reject ads using "
        "trademarked names, logos or celebrity likeness; describe the style ('inspired by') and never use official stills, "
        "logos or the actor's face in creatives. Time launches with releases/premieres.",
        "Studio product shot on an invisible mannequin, dark seamless background, dramatic rim light",
        "Anonymous model wearing it in a cinematic night scene, moody film-like colour grade",
        "Do not depict any real person, actor or film still; an anonymous model only."),
    "apparel": _cat(
        "Clothing (shirts, hoodies, dresses...)", "Fashion", 1.00, 1.05, 1.00, 45, "steady", {"gift", "men", "women", "eid"},
        "item", "18-45, interests: online shopping, fashion, streetwear, the store's style; broad Advantage+",
        "New in: {product} - made to be worn every day",
        "Fashion: show fit and real fabric; a clear size guide and easy returns lower refunds.",
        "Everyday clothing. Buyers decide on fit, fabric and price in seconds: show the item on a model, fabric close-ups "
        "and a size guide. Broad audiences with Advantage+ work; collections and carousels sell several items. Eid, "
        "Black Friday and end-of-season sales are the big moments.",
        "Flat-lay on a clean light background, neatly folded, soft daylight, fabric texture visible",
        "Model wearing it in a bright everyday street or cafe setting, natural light, relaxed pose"),
    "footwear": _cat(
        "Shoes & footwear", "Fashion", 1.00, 1.00, 0.95, 80, "steady", {"gift", "men", "women", "eid"}, "pair",
        "18-45, interests: sneakers, shoes, fashion, running or formal wear depending on the shoe",
        "Your next favourite pair: {product}",
        "Footwear: sizing is the top reason for returns; show a size chart and the sole and insole.",
        "Shoes and sandals. Show the pair from the side, the sole and on-foot shots; sizing confidence (size chart, easy "
        "exchange) lifts conversion. A carousel of angles (side, sole, on foot) works well.",
        "Product shot of the pair at a three-quarter angle on a plain light background, crisp shadow",
        "On-foot shot walking on a city pavement, low camera angle, natural light"),
    "bags_accessories": _cat(
        "Bags, wallets & belts", "Fashion", 1.00, 1.00, 0.95, 60, "steady", {"gift", "men", "women", "eid"}, "piece",
        "22-50, interests: handbags, leather goods, accessories, gifts",
        "Built to last: {product}",
        "Accessories: show size in hand or on the body and the inside; quality close-ups build trust.",
        "Bags, wallets, belts and small leather goods. Gift angle is strong (Father's Day, Valentine's, Christmas). Show "
        "size in context, inside pockets and stitching; carousel of colours.",
        "Studio shot on a light stone surface, soft side light, leather texture and stitching visible",
        "Carried by an anonymous model on a city street, natural light, product in focus"),
    "jewelry_watches": _cat(
        "Jewelry & watches", "Fashion", 1.10, 1.10, 0.80, 70, "gifting", {"gift", "men", "women", "eid"}, "piece",
        "22-55, interests: jewelry, watches, luxury goods, gifts; partners of men/women before gift days",
        "The gift they will actually wear: {product}",
        "Jewelry: claims about gold/silver/stones must be accurate; close-up photos must match the real item.",
        "Jewelry and watches. Mostly bought as gifts: Valentine's, Mother's Day, Eid and Christmas are peaks. Macro "
        "close-ups, on-hand or on-wrist shots and gift packaging sell; reassure on material and authenticity.",
        "Macro close-up on dark velvet, soft sparkle highlights, shallow depth of field",
        "Worn by an anonymous model, close crop on hand or neck, warm evening light"),
    "swimwear_summer": _cat(
        "Swimwear & summer wear", "Fashion", 1.00, 1.05, 0.95, 40, "summer", {"women", "men"}, "item",
        "18-40, interests: beach, travel, swimwear, summer fashion",
        "Ready for summer: {product}",
        "Swimwear: Meta limits skin-heavy images; keep poses natural and avoid suggestive crops.",
        "Swimwear and summer clothing. Demand peaks before summer and holidays; flipped in the southern hemisphere. "
        "Lifestyle beach/pool imagery, size inclusivity and fast shipping matter. Keep images within Meta's nudity rules.",
        "Flat-lay on warm sand-coloured background, bright sunlight, crisp shadows",
        "Anonymous model by a pool in bright summer light, relaxed and natural pose",
        "Keep the pose natural and modest; no suggestive crops."),
    "kitchen_cutlery": _cat(
        "Kitchen, cutlery & dining", "Home", 0.90, 0.95, 1.05, 60, "steady", {"gift", "women", "eid", "home"}, "set",
        "25-60, interests: cooking, kitchenware, home decor, hosting, new homes and weddings",
        "Make every meal look special: {product}",
        "Kitchen goods: show the full set and its size; state the material (stainless steel grade) and care honestly.",
        "Cutlery, kitchenware and dining sets. Bought for new homes, weddings and hosting, and as gifts: Eid (especially "
        "Eid al-Adha hosting), Mother's Day, Christmas and Black Friday are peaks. Show the full set laid out, a laid "
        "table, close-ups of finish and weight, and a gift box. Audiences: home makers, cooking and hosting interests.",
        "Full set laid out on a clean marble surface, top-down view, soft daylight, polished finish visible",
        "Elegant laid dinner table with the set in use, warm evening light, food in soft focus"),
    "home_decor": _cat(
        "Home decor & furnishing", "Home", 0.90, 0.95, 0.90, 70, "steady", {"gift", "home", "eid"}, "piece",
        "25-60, interests: interior design, home decor, new homes, DIY",
        "The finishing touch your room needs: {product}",
        "Decor: show real size in a room and true colours; returns of bulky items are costly.",
        "Home decor, textiles and furnishings. Show the item in a styled room for scale and mood plus a close-up of the "
        "material; new-home and seasonal refresh angles work.",
        "Product shot on a plain light background with a soft shadow, true colours",
        "Styled in a bright modern living room, natural window light, calm composition"),
    "beauty_skincare": _cat(
        "Beauty, skincare & perfume", "Beauty & health", 1.10, 1.10, 1.10, 35, "gifting", {"gift", "women", "eid"}, "product",
        "18-50, interests: skincare, beauty, makeup, perfume, self-care",
        "Your skin will thank you: {product}",
        "Beauty: no before/after claims or promises of medical results; Meta reviews these ads strictly.",
        "Beauty, skincare and fragrance. Texture shots and step-by-step routine carousels sell; gifting around Valentine's, "
        "Mother's Day, Eid and Christmas. Avoid before/after images and medical claims (Meta policy).",
        "Product bottle on a pastel surface with soft shadows and a few natural props, clean and bright",
        "Hands applying the product, soft morning light, fresh and natural mood",
        "No before/after comparison and no visible skin conditions."),
    "health_fitness": _cat(
        "Health & fitness", "Beauty & health", 1.05, 1.00, 1.00, 45, "steady", {"gift", "men", "women"}, "product",
        "18-50, interests: fitness, gym, running, yoga, healthy living",
        "Train smarter with {product}",
        "Health: no weight-loss or medical claims and no body-shaming before/after images (Meta policy).",
        "Fitness gear and wellness products. New-year and pre-summer motivation peaks; show the product in use and the "
        "benefit, never medical or weight-loss promises.",
        "Product shot on a clean gym floor background, crisp light",
        "Anonymous athlete using it in a bright gym or outdoors, motion, energetic mood"),
    "electronics_gadgets": _cat(
        "Electronics & gadgets", "Other", 1.15, 1.00, 0.85, 60, "gifting", {"gift", "men"}, "device",
        "18-50, interests: technology, gadgets, smartphones, gaming",
        "The gadget you did not know you needed: {product}",
        "Electronics: state compatibility, warranty and what is in the box; misleading spec claims get ads rejected.",
        "Gadgets and accessories. Carousels that show the problem and the fix step by step convert best; Black Friday and Christmas are "
        "huge; compatibility and warranty reassure.",
        "Product shot on a dark desk surface, subtle rim light, sleek reflections",
        "In use on a modern desk or in hand, cool daylight, tidy background"),
    "kids_toys": _cat(
        "Kids & toys", "Other", 1.00, 1.05, 1.00, 35, "gifting", {"gift", "eid"}, "toy",
        "25-45 parents, interests: parenting, toys, kids education, gifts",
        "Hours of fun: {product}",
        "Kids: ads must target parents, not children; state the age range and safety standards.",
        "Toys and kids products. Target parents and gift-givers; Christmas, birthdays and Eid peaks. Show play in action "
        "(hands only), age range and safety.",
        "Colourful product shot on a bright plain background, playful soft shadows",
        "Toy in use on a bright playroom floor, only hands visible, cheerful light",
        "No children's faces; hands only or the product alone."),
    "pets": _cat(
        "Pet products", "Other", 0.90, 1.10, 1.05, 35, "steady", {"gift"}, "product",
        "22-55, interests: dogs, cats, pet care, pet owners",
        "Made for the one who waits for you at home: {product}",
        "Pets: show the product used safely; avoid health claims for supplements.",
        "Pet supplies. Pet owners respond to emotional, cute in-use photos and carousels; repeat purchase is common. Gift angle at "
        "Christmas.",
        "Product shot on a soft neutral background, friendly warm light",
        "A happy dog or cat using it at home, warm natural light"),
    "other": _cat(
        "Other (describe it)", "Other", 1.00, 1.00, 1.00, 60, "steady", {"gift"}, "product",
        "18-55, broad Advantage+; interests that match the product",
        "Meet {product}",
        "Check Meta's advertising policies for this product type before launch.",
        "A product outside the listed categories: use the user's description in INPUTS.category_note to infer the "
        "buyer, the angle and any policy limits; prefer broad Advantage+ audiences while learning.",
        "Clean product shot on a plain light background, soft shadow",
        "The product in use in a natural, bright everyday setting"),
}
DEFAULT_NICHE = "leather_jackets"
NICHE_MULTIPLIERS = CATEGORIES  # older name

# Price elasticity: a pricier item converts less. Bands are for an item at the category's typical
# price (150 USD for jackets); each category's bands are scaled to its own typical price.
PRICE_BANDS = [
    (60, 1.30),
    (100, 1.10),
    (150, 1.00),
    (220, 0.80),
    (350, 0.60),
    (10**9, 0.40),
]


def price_multiplier(price_usd: float, typical_price: float = 150) -> float:
    scale = (typical_price or 150) / 150
    for ceiling, mult in PRICE_BANDS:
        if price_usd <= ceiling * scale:
            return mult
    return 0.30


def get_country(code: str) -> dict:
    code = code.upper()
    if code not in COUNTRY_BENCHMARKS:
        raise KeyError(f"No benchmark for country '{code}'. Known: {', '.join(COUNTRY_BENCHMARKS)}")
    return COUNTRY_BENCHMARKS[code]


def get_niche(niche: str) -> dict:
    return CATEGORIES.get((niche or "").lower(), CATEGORIES[DEFAULT_NICHE])


get_category = get_niche
