/**
 * Aircraft photos from Planespotters.net's public photo API.
 *
 * Their terms: the visitor's browser loads the image straight from their servers (no copying
 * or proxying), the photographer is credited next to it, the photo links to its page with a
 * plain link, and API answers may be cached for up to 24 hours.
 */

export interface Photo {
  src: string;
  width: number;
  height: number;
  link: string;
  photographer: string;
}

const API = "https://api.planespotters.net/pub/photos";
const CACHE_MS = 24 * 3600 * 1000;
const cache = new Map<string, Promise<Photo | null>>();

function stored(key: string): Photo | null | undefined {
  try {
    const raw = localStorage.getItem(`photo:${key}`);
    if (!raw) return undefined;
    const { at, photo } = JSON.parse(raw);
    return Date.now() - at < CACHE_MS ? photo : undefined;
  } catch {
    return undefined;
  }
}

function store(key: string, photo: Photo | null): void {
  try {
    localStorage.setItem(`photo:${key}`, JSON.stringify({ at: Date.now(), photo }));
  } catch {
    // storage full or blocked: just don't cache
  }
}

async function lookup(path: string): Promise<Photo | null> {
  const res = await fetch(`${API}/${path}`);
  if (!res.ok) return null;
  const first = (await res.json()).photos?.[0];
  if (!first) return null;
  const image = first.thumbnail_large ?? first.thumbnail;
  return {
    src: image.src,
    width: image.size?.width ?? 420,
    height: image.size?.height ?? 280,
    link: first.link,
    photographer: first.photographer,
  };
}

/** A photo of this exact airframe (by transponder code, then registration), or null. */
export function photoFor(hex: string, registration: string | null): Promise<Photo | null> {
  const key = hex.toLowerCase();
  let pending = cache.get(key);
  if (!pending) {
    const saved = stored(key);
    pending =
      saved !== undefined
        ? Promise.resolve(saved)
        : (async () => {
            try {
              const photo =
                (await lookup(`hex/${encodeURIComponent(key)}`)) ??
                (registration ? await lookup(`reg/${encodeURIComponent(registration)}`) : null);
              store(key, photo);
              return photo;
            } catch {
              return null; // offline or blocked: no photo, no fuss
            }
          })();
    cache.set(key, pending);
  }
  return pending;
}

/** Synchronous peek, so re-rendering the card every few seconds doesn't flicker the photo. */
export function cachedPhoto(hex: string): Photo | null | undefined {
  return stored(hex.toLowerCase());
}
