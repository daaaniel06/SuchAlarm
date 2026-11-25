(source, limit) => {
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
      description: '.aditem-main--middle--description',
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
  const textWithout = (node, selector) => {
    const copy = node.cloneNode(true);
    copy.querySelectorAll(selector).forEach(el => el.remove());
    return copy.textContent.trim();
  };
  const shipping = card => {
    const container = card.querySelector(c.shipping);
    return source === 'kleinanzeigen' && container ? textWithout(container, c.price) : text(card, c.shipping);
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
    const pattern = /\\?"itemId\\?":\\?"?(\d+)\\?"?\}\},\\?"catalogTracking\\?":\{[^{}]{0,200}?\\?"ownerId\\?":(\d+)/g;
    for (const [, item, owner] of data.matchAll(pattern)) vintedOwners.set(item, owner);
  }
  const owner = card =>
    vintedOwners.get(card.querySelector('[data-testid^="product-item-id-"]')?.dataset.testid.match(/\d+/)?.[0]) || '';
  // Fallback when the selectors miss (renamed classes): cards are found by their detail links, title and price
  // read from the card's text. A row names what came from there under fromText, which sources.py logs.
  const detailLink = {
    ebay: /\/itm\/\d+/,
    kleinanzeigen: /\/s-anzeige\/.+\/\d+-\d+-\d+/,
    vinted: /\/items\/\d+/,
  }[source];
  const offerOf = a => a.href.split(/[?#]/)[0];
  // The largest element around each detail link that holds no other offer.
  const cardsByLinks = () => {
    if (!detailLink) return [];
    const links = [...document.querySelectorAll('a[href]')].filter(a => detailLink.test(a.pathname));
    const cards = new Set();
    for (const a of links) {
      let card = a;
      const alone = node => links.every(other => !node.contains(other) || offerOf(other) === offerOf(a));
      while (card.parentElement && card.parentElement !== document.body && alone(card.parentElement))
        card = card.parentElement;
      cards.add(card);
    }
    return [...cards];
  };
  // The one euro amount of a card besides shipping and fees; none when it names two (a struck-through price) or a
  // range ("20 bis 30 €"), so a fallback never guesses between prices. Fees: "+ 4,50 € Versand", "Versand: 4,99 €",
  // "inkl. Käuferschutz 46,90 €", "46,90 € inkl.".
  const FEE_AMOUNT =
    /(?:\+|versand\w*|lieferung|shipping|käuferschutz|inkl\.?)[^\d€]{0,15}(?:EUR\s*)?\d[\d.,]*(?:\s*€)?|\d[\d.,]*\s*(?:€|EUR)\s*inkl/gi;
  const PRICE_RANGE =
    /(?:€|\bEUR)\s*\d[\d.,]*\s*(?:bis|[-–])|\d[\d.,]*\s*(?:€|EUR\b)?\s*(?:bis|[-–])\s*\d[\d.,]*\s*(?:€|EUR\b)/i;
  const lonePrice = cardText => {
    const rest = cardText.replace(FEE_AMOUNT, ' ');
    if (PRICE_RANGE.test(rest)) return '';
    const amounts = new Set(
      [...rest.matchAll(/(?:€|\bEUR)\s*(\d[\d.,]*)|(\d[\d.,]*)\s*(?:€|EUR\b)/g)].map(found => found[1] || found[2]),
    );
    return amounts.size === 1 ? `${[...amounts][0]} €` : '';
  };
  // A card's text without headings and its own parts, its text nodes spaced: textContent would join a title
  // "… PS5" and "29,99 €" to "PS529,99 €".
  const spacedText = card => {
    const copy = card.cloneNode(true);
    copy.querySelectorAll(`${ownParts}, h1, h2, h3, h4`).forEach(el => el.remove());
    const walker = document.createTreeWalker(copy, NodeFilter.SHOW_TEXT);
    const parts = [];
    while (walker.nextNode()) parts.push(walker.currentNode.textContent);
    return parts.join(' ');
  };
  const heading = card => card.querySelector('h1, h2, h3, h4')?.textContent.trim() || '';
  // What the card says about the offer: without its title and the seller's own description.
  const ownParts = [c.title, c.description, 'script, style'].filter(Boolean).join(', ');
  const byClass = [...document.querySelectorAll(c.cards)];
  const cards = byClass.length ? byClass : cardsByLinks();
  const rows = cards.slice(0, limit).map(card => {
    const a = card.querySelector(c.link) || (card.matches('a[href]') ? card : null);
    const fromText = byClass.length ? [] : ['card'];
    const status = textWithout(card, ownParts).slice(0, 2500);
    // Vinted's card title is only the brand since 11/2026; the link's title holds the item's own title
    // before ", Marke: …, Zustand: …, 30.00 €".
    const ownTitle =
      (source === 'vinted' && a?.title.split(/, (?:Marke|Zustand|Größe): /)[0].trim()) || title(card) || a?.title || '';
    const ownPrice = text(card, c.price);
    const titleFromText = ownTitle ? '' : heading(card) || a?.textContent.trim() || '';
    const priceFromText = ownPrice ? '' : lonePrice(spacedText(card));
    if (titleFromText) fromText.push('title');
    if (priceFromText) fromText.push('price');
    let shippingText = '';
    if (source === 'ebay') {
      shippingText = textWithout(
        card,
        `${c.title}, ${c.price}, ${c.seller}, .s-card__subtitle, .clipped, .sr-only, .visually-hidden, [hidden], script, style`,
      ).slice(0, 2500);
    }
    return {
      shippingText,
      title: ownTitle || titleFromText,
      price: ownPrice || priceFromText,
      url: a?.href || '',
      seller: source === 'ebay' ? ebaySeller(card) : text(card, c.seller),
      owner: owner(card),
      condition: source === 'ebay' ? ebayCondition(card) : text(card, c.condition),
      shipping: shipping(card),
      location:
        source === 'ebay'
          ? text(card, '.s-item__location, .s-item__itemLocation, .s-card__location, .s-card__itemLocation')
          : '',
      image: picture(card),
      text: card.textContent.slice(0, 2500),
      status,
      fromText,
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
          const details = [].concat(offer.shippingDetails || []);
          const detail =
            details.find(entry => entry.shippingDestination?.addressCountry === 'DE') ||
            (details.length === 1 && !details[0].shippingDestination?.addressCountry ? details[0] : null);
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
