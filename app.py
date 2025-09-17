def _place_mexc_market(symbol: str, side: str, budget_quote: float, price_hint: float = 0.0):
    """
    Robuuste market order:
    - BUY: eerst quoteOrderQty (budget in USDT), met 'cost' als extra hint;
           valt zo nodig terug naar quantity = budget / price (gepreciseerd).
    - SELL: stuurt quantity (base), met precision.
    """
    ex = _mexc_live()
    side_l = (side or "").lower()
    if side_l not in ("buy", "sell"):
        raise ValueError("side must be 'buy' of 'sell'")

    m = ex.market(symbol)
    # limits (kunnen 0/None zijn op MEXC, dus niet blind vertrouwen)
    try:
        min_amt  = float((((m.get("limits") or {}).get("amount") or {}).get("min") or 0.0) or 0.0)
    except Exception:
        min_amt = 0.0

    params = {"recvWindow": _recvwin()}

    if side_l == "buy":
        spend = float(budget_quote or 0.0)
        if spend <= 0:
            raise ValueError("budget_quote moet > 0 zijn voor market BUY")

        # --- 1) Primary: quoteOrderQty (en 'cost' als hint) ---
        qparams = dict(params)
        try:
            spend_prec = float(ex.price_to_precision(symbol, spend))
        except Exception:
            spend_prec = float(round(spend, 6))
        qparams["quoteOrderQty"] = spend_prec
        qparams["cost"] = spend_prec  # sommige exchanges/versies lezen dit

        try:
            return ex.create_order(symbol, "market", "buy", None, None, qparams)
        except Exception as e1:
            # --- 2) Fallback: quantity = budget / last (of price_hint) ---
            p = float(price_hint or 0.0)
            if p <= 0:
                try:
                    t = ex.fetch_ticker(symbol)
                    p = float(t.get("last") or t.get("close") or 0.0)
                except Exception:
                    p = 0.0
            if p <= 0:
                # geen prijs → laat de except bubbelen (caller handelt af)
                raise e1

            qty = spend / max(p, 1e-12)
            try:
                qtyp = float(ex.amount_to_precision(symbol, qty))
            except Exception:
                qtyp = float(round(qty, 6))

            if qtyp <= 0:
                raise e1
            if min_amt and qtyp < min_amt:
                qtyp = float(min_amt)

            return ex.create_order(symbol, "market", "buy", qtyp, None, params)

    else:
        # SELL: gebruik quantity (base)
        qty = float(budget_quote or 0.0)  # hier is budget_quote de gewenste base-hoeveelheid of 0
        if qty <= 0 and price_hint > 0:
            # als iemand per ongeluk quote meegaf, reken terug naar base
            qty = float(budget_quote / max(price_hint, 1e-12))
        if min_amt and 0 < qty < min_amt:
            qty = float(min_amt)
        try:
            amount = float(ex.amount_to_precision(symbol, qty)) if qty > 0 else None
        except Exception:
            amount = float(round(qty, 6)) if qty > 0 else None
        return ex.create_order(symbol, "market", "sell", amount, None, params)
