def get_score(price, liquidity, days_left):

    score = 0

    # Чем дешевле, тем лучше
    if price <= 0.002:
        score += 50
    elif price <= 0.005:
        score += 40
    elif price <= 0.01:
        score += 30

    # Чем длиннее срок, тем лучше
    if days_left >= 730:
        score += 30
    elif days_left >= 365:
        score += 20
    elif days_left >= 180:
        score += 10

    # Ликвидность
    if liquidity >= 100000:
        score += 20
    elif liquidity >= 10000:
        score += 10

    return min(score, 100)

def get_rarity(score):

    if score >= 98:
        return "👑 MYTHIC"

    if score >= 95:
        return "🚨 LEGENDARY"

    if score >= 90:
        return "🔥 EPIC"

    if score >= 80:
        return "🟢 RARE"

    return "⚪ COMMON"