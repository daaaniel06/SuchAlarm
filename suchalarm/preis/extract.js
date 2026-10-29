source => {
  const configs = {
    ebay: {
      cards: '.s-item, .s-card',
      title: '.s-item__title, .s-card__title',
      price: '.s-item__price, .s-card__price',
      link: 'a.s-item__link, a.s-card__link, a[href*="/itm/"]',
      seller: '.s-item__seller-info-text',
      condition: '.SECONDARY_INFO',
      shipping: '.s-item__shipping, .s-item__logisticsCost, .s-card__shipping',
    },
    kleinanzeigen: {
      cards: 'article.aditem, article[data-adid]',
      title: 'h3, h2, .ellipsis',
      price: '.aditem-main--middle--price-shipping--price, p[class*="text-title3"]',
      link: 'a.ellipsis, a[href*="/s-anzeige/"]',
      seller: '.aditem-main--top--left',
      condition: '.simpletag',
      shipping: '.aditem-main--middle--price-shipping',
    },
    vinted: {
      cards: '[data-testid="grid-item"]',
      title: '[data-testid$="--description-title"], a[href*="/items/"]',
      price: '[data-testid$="--price-text"], [data-testid="item-price"]',
      link: 'a[href*="/items/"]',
      seller: '[data-testid$="--owner"]',
      condition: '[data-testid$="--description-subtitle"]',
      shipping:
        '[data-testid="shipping-price"], [data-testid$="--shipping-price"], [data-testid="item-shipping-price"]',
    },
    idealo: {
      cards: '[data-testid="offer-list-item"], .productOffers-listItem, .productOffers-listItemOffer',
      title: '[data-testid="offer-title"], .productOffers-listItemTitle',
      price: '[data-testid="offer-price"], .productOffers-listItemOfferPrice',
      link: 'a[href*="/preisvergleich/OffersOfProduct/"]',
      seller: '[data-testid="shop-name"], .productOffers-listItemOfferShop',
      condition: '',
      shipping: '[data-testid="shipping-cost"], .productOffers-listItemOfferShipping',
    },
  };
  const c = configs[source];
  const text = (card, selector) => {
    for (const part of (selector || '').split(',').filter(Boolean)) {
      const value = card.querySelector(part.trim())?.textContent?.trim();
      if (value) return value;
    }
    return '';
  };
  const title = card => {
    if (source !== 'ebay') return text(card, c.title);
    for (const selector of c.title.split(',')) {
      const node = card.querySelector(selector.trim());
      if (!node) continue;
      const copy = node.cloneNode(true);
      copy
        .querySelectorAll(
          '.clipped, .sr-only, .visually-hidden, [hidden], .s-item__title--tagblock, .s-card__title--tagblock',
        )
        .forEach(el => el.remove());
      const value = copy.textContent.trim();
      if (value) return value;
    }
    return '';
  };
  // New eBay cards: subtitles hold platform and "Gebraucht | Privat", the seller is the row with its rating.
  const ebayCondition = card =>
    text(card, c.condition) ||
    (
      [...card.querySelectorAll('.s-card__subtitle')]
        .map(el => el.textContent.trim())
        .find(t => /^(?:neu|brandneu|gebraucht|generalüberholt|als ersatzteil|nicht funktionsfähig)\b/i.test(t)) || ''
    ).split(' | ')[0];
  const ebaySeller = card =>
    text(card, c.seller) ||
    (
      [...card.querySelectorAll('.s-card__attribute-row')]
        .map(el => el.textContent.trim())
        .find(t => /\d+(?:,\d+)?\s*%\s*positiv/i.test(t)) || ''
    ).replace(/\s+\d+(?:,\d+)?\s*%\s*positiv.*$/i, '');
  // Images are not loaded while reading, so lazy attributes and srcset carry the real address;
  // placeholders (data:, svg, gif) are skipped. From a srcset the smallest candidate of at least 300 px wins.
  const fromSet = set => {
    const items = (set || '')
      .split(',')
      .map(x => x.trim().split(/\s+/))
      .filter(x => x[0])
      .map(([url, size]) => ({ url, w: parseInt(size) || 0 }));
    const wide = items.filter(x => x.w >= 300).sort((a, b) => a.w - b.w);
    return (wide[0] || items[items.length - 1])?.url || '';
  };
  const picture = card => {
    const candidates = [];
    for (const el of card.querySelectorAll('img, picture source')) {
      for (const name of ['data-src', 'data-original', 'data-lazy-src', 'data-imgsrc'])
        candidates.push(el.getAttribute(name));
      candidates.push(
        fromSet(el.getAttribute('data-srcset')),
        fromSet(el.getAttribute('srcset')),
        el.currentSrc,
        el.getAttribute('src'),
      );
    }
    for (const value of candidates) {
      if (!value) continue;
      try {
        const url = new URL(value, location.href);
        if (url.protocol === 'https:' && !/\.(svg|gif)$/i.test(url.pathname)) return url.href;
      } catch {}
    }
    return '';
  };
  // Vinted names no seller on its cards. The page's embedded data (script text, still there after hydration)
  // follows each item ID with a tracking block holding the owner ID (live layout of 29.10.2026).
  const vintedOwners = new Map();
  if (source === 'vinted') {
    const data = [...document.scripts].map(script => script.textContent).join('');
    const pattern =
      /\\?"itemId\\?":\\?"?(\d+)\\?"?\}\},\\?"catalogTracking\\?":\{[^{}]{0,200}?\\?"ownerId\\?":(\d+)/g;
    for (const [, item, owner] of data.matchAll(pattern)) vintedOwners.set(item, owner);
  }
  const owner = card =>
    vintedOwners.get(card.querySelector('[data-testid^="product-item-id-"]')?.dataset.testid.match(/\d+/)?.[0]) || '';
  const rows = [...document.querySelectorAll(c.cards)].slice(0, 100).map(card => {
    const a = card.querySelector(c.link);
    let shippingText = '';
    if (source === 'ebay') {
      const copy = card.cloneNode(true);
      copy
        .querySelectorAll(
          `${c.title}, ${c.price}, ${c.seller}, .s-card__subtitle, .clipped, .sr-only, .visually-hidden, [hidden], script, style`,
        )
        .forEach(el => el.remove());
      shippingText = copy.textContent.slice(0, 2500);
    }
    return {
      shippingText,
      title: title(card) || a?.title || '',
      price: text(card, c.price),
      url: a?.href || '',
      seller: source === 'ebay' ? ebaySeller(card) : text(card, c.seller),
      owner: owner(card),
      condition: source === 'ebay' ? ebayCondition(card) : text(card, c.condition),
      shipping: text(card, c.shipping),
      location:
        source === 'ebay'
          ? text(card, '.s-item__location, .s-item__itemLocation, .s-card__location, .s-card__itemLocation')
          : '',
      image: picture(card),
      text: card.textContent.slice(0, 2500),
    };
  });
  const structured = [];
  function visit(v, depth = 0) {
    if (!v || typeof v !== 'object' || depth > 15) return;
    if (Array.isArray(v)) {
      v.forEach(x => visit(x, depth + 1));
      return;
    }
    const types = [].concat(v['@type'] || []);
    if (types.includes('Product') && v.name && v.offers) {
      const pending = [].concat(v.offers);
      for (const offer of pending) {
        if (offer.offers) pending.push(...[].concat(offer.offers));
        // AggregateOffer is a comparison value, not an actual purchasable offer.
        if (offer.lowPrice)
          structured.push({
            reference: true,
            title: v.name,
            price: offer.lowPrice,
            currency: offer.priceCurrency,
            url: v.url || location.href,
          });
        if (
          offer.price &&
          offer.priceCurrency === 'EUR' &&
          /InStock$|LimitedAvailability$/.test(offer.availability || '')
        ) {
          const detail = [].concat(offer.shippingDetails || [])[0];
          structured.push({
            title: v.name,
            price: offer.price,
            currency: 'EUR',
            url: v.url || location.href,
            image: [].concat(v.image || [])[0]?.url || [].concat(v.image || [])[0] || '',
            seller: offer.seller?.name || '',
            condition: offer.itemCondition || '',
            shippingAmount: detail?.shippingRate?.currency === 'EUR' ? detail.shippingRate.value : null,
          });
        }
      }
    }
    Object.values(v).forEach(x => visit(x, depth + 1));
  }
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      visit(JSON.parse(script.textContent));
    } catch {}
  }
  const links =
    source === 'idealo'
      ? [
          ...new Map(
            [...document.querySelectorAll('a[href*="/preisvergleich/OffersOfProduct/"]')].map(a => [
              a.href,
              { url: a.href, title: a.textContent.trim() || a.title || a.querySelector('img')?.alt || '' },
            ]),
          ).values(),
        ].slice(0, 40)
      : [];
  const vintedDetail =
    source === 'vinted' &&
    /^\/items\/\d+/.test(location.pathname) &&
    !!document.querySelector('[data-testid="item-title"], [data-testid="item-price"]');
  let vintedShipping = '';
  if (vintedDetail) {
    vintedShipping = text(
      document,
      '[data-testid="item-shipping-banner"], [data-testid="item-shipping-banner-price"], [data-testid="item-shipping-price"], [data-testid="shipping-price"], [data-testid="item-shipping"]',
    );
    if (!vintedShipping) {
      // The delivery summary uses changing test IDs. Read labelled visible lines,
      // excluding the seller's description and other recommended item cards.
      const copy = document.body.cloneNode(true);
      copy
        .querySelectorAll(
          '[data-testid="item-description"], [data-testid="item-title"], [data-testid="grid-item"], script, style, [hidden]',
        )
        .forEach(el => el.remove());
      vintedShipping = [...copy.querySelectorAll('p, span, div')]
        .filter(
          el => !el.children.length && /^(?:versand|lieferung|shipping)(?:kosten)?\b/i.test(el.textContent.trim()),
        )
        .map(el =>
          [...el.parentElement.childNodes]
            .map(n => n.textContent)
            .join(' ')
            .trim(),
        )
        .join('\n')
        .slice(0, 2000);
    }
  }
  return {
    rows,
    structured,
    links,
    vintedDetail,
    vintedShipping,
    text: (document.body?.innerText || '').slice(0, 14000),
    image: document.querySelector('meta[property="og:image"]')?.content || '',
    title: document.title,
  };
};
