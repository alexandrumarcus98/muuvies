export type CorridorCategory = 'trending' | 'archival';

export interface CorridorMediaItem {
  id: string;
  index: number;
  title: string;
  director: string;
  year: string;
  aspect: string;
  specs: string;
  synopsis: string;
  imageSrc: string;
  /** World-space placement along the Z-axis corridor */
  position: {
    x: number;
    y: number;
    z: number;
  };
  /** Normalized plane dimensions in world units */
  size: {
    width: number;
    height: number;
  };
  /** Normalized spline progress [0, 1] inside the corridor segment */
  progress: number;
}

const SLOTS: ReadonlyArray<{
  position: { x: number; y: number; z: number };
  size: { width: number; height: number };
  progress: number;
}> = [
  { position: { x: -2.6, y: 0.45, z: -2 }, size: { width: 4.8, height: 3.4 }, progress: 0.04 },
  { position: { x: 2.8, y: -0.4, z: -16 }, size: { width: 4.8, height: 3.4 }, progress: 0.145 },
  { position: { x: -3.0, y: -0.3, z: -30 }, size: { width: 4.9, height: 3.45 }, progress: 0.25 },
  { position: { x: 2.5, y: 0.55, z: -44 }, size: { width: 4.8, height: 3.4 }, progress: 0.355 },
  { position: { x: -2.4, y: 0.5, z: -58 }, size: { width: 5.0, height: 3.45 }, progress: 0.46 },
  { position: { x: 3.1, y: -0.25, z: -72 }, size: { width: 5.1, height: 3.5 }, progress: 0.565 },
  { position: { x: -2.9, y: -0.55, z: -86 }, size: { width: 5.4, height: 3.1 }, progress: 0.67 },
  { position: { x: 2.7, y: 0.4, z: -100 }, size: { width: 5.0, height: 3.3 }, progress: 0.775 },
  { position: { x: -2.5, y: 0.6, z: -114 }, size: { width: 5.3, height: 3.15 }, progress: 0.88 },
  { position: { x: 0.0, y: 0.0, z: -128 }, size: { width: 5.6, height: 3.3 }, progress: 0.985 },
];

/**
 * Weekly most-wanted / top-rated showcase.
 * Replace imageSrc / entries via `/api/public/trending` when a TMDB feed is connected.
 */
export const TRENDING_MEDIA: readonly CorridorMediaItem[] = [
  {
    id: 'dune-part-two',
    index: 1,
    title: 'Dune: Part Two',
    director: 'Denis Villeneuve',
    year: '2024',
    aspect: '2.39:1',
    specs: '4K Blu-ray · Dolby Vision · TrueHD Atmos',
    synopsis:
      'Paul Atreides unites with Chani and the Fremen while seeking revenge against the conspirators who destroyed his family.',
    imageSrc: '/static/media/ribbon/red-metropolis.jpg',
    ...SLOTS[0],
  },
  {
    id: 'oppenheimer',
    index: 2,
    title: 'Oppenheimer',
    director: 'Christopher Nolan',
    year: '2023',
    aspect: '2.20:1',
    specs: '4K UHD · IMAX 65mm · DTS-HD MA 5.1',
    synopsis:
      'The story of J. Robert Oppenheimer’s role in the development of the atomic bomb during World War II.',
    imageSrc: '/static/media/ribbon/bw-metropolis.jpg',
    ...SLOTS[1],
  },
  {
    id: 'blade-runner-2049',
    index: 3,
    title: 'Blade Runner 2049',
    director: 'Denis Villeneuve',
    year: '2017',
    aspect: '2.39:1',
    specs: '4K Remux · HDR10 · TrueHD Atmos',
    synopsis:
      'Young Blade Runner K’s discovery of a long-buried secret leads him to track down former Blade Runner Rick Deckard.',
    imageSrc: '/static/media/ribbon/red-nosferatu.jpg',
    ...SLOTS[2],
  },
  {
    id: 'nosferatu-2024',
    index: 4,
    title: 'Nosferatu',
    director: 'Robert Eggers',
    year: '2024',
    aspect: '1.66:1',
    specs: '4K UHD · Dolby Vision · TrueHD Atmos',
    synopsis:
      'A gothic tale of obsession between a haunted young woman and the terrifying vampire infatuated with her.',
    imageSrc: '/static/media/ribbon/bw-nosferatu.jpg',
    ...SLOTS[3],
  },
  {
    id: 'there-will-be-blood',
    index: 5,
    title: 'There Will Be Blood',
    director: 'Paul Thomas Anderson',
    year: '2007',
    aspect: '2.39:1',
    specs: '4K Master · 35mm · LPCM 5.1',
    synopsis:
      'A story of family, religion, hatred, oil and madness, focusing on a turn-of-the-century prospector in the early days of the business.',
    imageSrc: '/static/media/ribbon/red-general.jpg',
    ...SLOTS[4],
  },
  {
    id: 'the-lighthouse',
    index: 6,
    title: 'The Lighthouse',
    director: 'Robert Eggers',
    year: '2019',
    aspect: '1.19:1',
    specs: '4K UHD · Orthochromatic · DTS-HD MA',
    synopsis:
      'Two lighthouse keepers try to maintain their sanity while living on a remote and mysterious New England island in the 1890s.',
    imageSrc: '/static/media/ribbon/bw-caligari.jpg',
    ...SLOTS[5],
  },
  {
    id: 'mad-max-fury-road',
    index: 7,
    title: 'Mad Max: Fury Road',
    director: 'George Miller',
    year: '2015',
    aspect: '2.39:1',
    specs: '4K UHD · HDR10 · TrueHD Atmos',
    synopsis:
      'In a post-apocalyptic wasteland, a woman rebels against a tyrannical ruler in search for her homeland with the aid of a group of female prisoners.',
    imageSrc: '/static/media/ribbon/red-caligari.jpg',
    ...SLOTS[6],
  },
  {
    id: 'in-the-mood-for-love',
    index: 8,
    title: 'In the Mood for Love',
    director: 'Wong Kar-wai',
    year: '2000',
    aspect: '1.66:1',
    specs: '4K Criterion · LPCM 2.0',
    synopsis:
      'Two neighbors in 1962 Hong Kong form a bond after both suspect extramarital activities of their spouses.',
    imageSrc: '/static/media/ribbon/red-moon.jpg',
    ...SLOTS[7],
  },
  {
    id: 'stalker',
    index: 9,
    title: 'Stalker',
    director: 'Andrei Tarkovsky',
    year: '1979',
    aspect: '1.37:1',
    specs: '4K Restoration · Mosfilm · Mono PCM',
    synopsis:
      'A guide leads two men through an area known as the Zone to find a room that grants wishes.',
    imageSrc: '/static/media/ribbon/bw-general.jpg',
    ...SLOTS[8],
  },
  {
    id: 'arrival',
    index: 10,
    title: 'Arrival',
    director: 'Denis Villeneuve',
    year: '2016',
    aspect: '2.39:1',
    specs: '4K UHD · Dolby Vision · TrueHD Atmos',
    synopsis:
      'A linguist works with the military to communicate with alien lifeforms after twelve mysterious spacecraft appear around the world.',
    imageSrc: '/static/media/ribbon/bw-moon.jpg',
    ...SLOTS[9],
  },
];

export const ARCHIVAL_MEDIA: readonly CorridorMediaItem[] = [
  {
    id: 'trip-to-the-moon',
    index: 1,
    title: 'A Trip to the Moon',
    director: 'Georges Méliès',
    year: '1902',
    aspect: '1.33:1',
    specs: '4K restoration · 1.33:1 · Silent PCM',
    synopsis:
      'Hand-tinted nitrate print of the Star Film lunar expedition—the foundational grammar of matte compositing, practical stagecraft, and surrealist cinema.',
    imageSrc: '/static/media/ribbon/bw-moon.jpg',
    ...SLOTS[0],
  },
  {
    id: 'cabinet-caligari',
    index: 2,
    title: 'The Cabinet of Dr. Caligari',
    director: 'Robert Wiene',
    year: '1920',
    aspect: '1.33:1',
    specs: '4K UHD · 1.33:1 · Mono archival',
    synopsis:
      'Jagged Expressionist corridors and painted chiaroscuro shadows construct an unreliable narrator’s fever dream of hypnosis and murder.',
    imageSrc: '/static/media/ribbon/bw-caligari.jpg',
    ...SLOTS[1],
  },
  {
    id: 'nosferatu-1922',
    index: 3,
    title: 'Nosferatu',
    director: 'F. W. Murnau',
    year: '1922',
    aspect: '1.33:1',
    specs: '4K master · 1.33:1 · Tinted nitrate',
    synopsis:
      'Location-shot Baltic dread where Count Orlok’s silhouette ascends the stairwell, turning natural light and silver halide grain into pure menace.',
    imageSrc: '/static/media/ribbon/bw-nosferatu.jpg',
    ...SLOTS[2],
  },
  {
    id: 'safety-last',
    index: 4,
    title: 'Safety Last!',
    director: 'Fred C. Newmeyer & Sam Taylor',
    year: '1923',
    aspect: '1.33:1',
    specs: '4K UHD · 1.33:1 · LPCM 2.0',
    synopsis:
      'Forced-perspective rooftop rigs above downtown Los Angeles turn a department-store climb into an unforgettable study in physical comedy and vertigo.',
    imageSrc: '/static/media/ribbon/bw-safety-last.jpg',
    ...SLOTS[3],
  },
  {
    id: 'the-general',
    index: 5,
    title: 'The General',
    director: 'Buster Keaton & Clyde Bruckman',
    year: '1926',
    aspect: '1.37:1',
    specs: '4K scan · 1.37:1 · TrueHD mono',
    synopsis:
      'Unbroken wide tracking shots along Oregon timberlines choreograph a full-scale locomotive pursuit with mathematical precision.',
    imageSrc: '/static/media/ribbon/bw-general.jpg',
    ...SLOTS[4],
  },
  {
    id: 'metropolis',
    index: 6,
    title: 'Metropolis',
    director: 'Fritz Lang',
    year: '1927',
    aspect: '1.33:1',
    specs: '4K complete · 1.33:1 · DTS-HD MA',
    synopsis:
      'Monumental Schüfftan-process skyscrapers and subterranean machine halls pit industrial futurism against the human pulse of the city.',
    imageSrc: '/static/media/ribbon/bw-metropolis.jpg',
    ...SLOTS[5],
  },
  {
    id: 'metropolis-crimson',
    index: 7,
    title: 'Metropolis: Babel Study',
    director: 'Fritz Lang',
    year: '1927',
    aspect: '2.39:1',
    specs: '4K UHD · 2.39:1 · TrueHD Atmos',
    synopsis:
      'High-contrast crimson separation print isolating the Machine-Man transformation sequence and Karl Freund’s expressionist lighting ratios.',
    imageSrc: '/static/media/ribbon/red-metropolis.jpg',
    ...SLOTS[6],
  },
  {
    id: 'nosferatu-vermilion',
    index: 8,
    title: 'Nosferatu: Midnight Reel',
    director: 'F. W. Murnau',
    year: '1922',
    aspect: '1.85:1',
    specs: '4K UHD · 1.85:1 · Dolby Vision',
    synopsis:
      'Amber-and-vermilion nocturnal tinting study capturing the Empusa’s ghost voyage into Wisborg harbor before dawn.',
    imageSrc: '/static/media/ribbon/red-nosferatu.jpg',
    ...SLOTS[7],
  },
  {
    id: 'caligari-red-plate',
    index: 9,
    title: 'Caligari: Holstenwall Plate',
    director: 'Robert Wiene',
    year: '1920',
    aspect: '2.39:1',
    specs: '4K archive · 2.39:1 · LPCM 5.1',
    synopsis:
      'Deep-shadow orthochromatic study of Hermann Warm’s angular set flats, emphasizing diagonal sightlines and claustrophobic framing.',
    imageSrc: '/static/media/ribbon/red-caligari.jpg',
    ...SLOTS[8],
  },
  {
    id: 'general-iron-horse',
    index: 10,
    title: 'The General: Iron & Steam',
    director: 'Buster Keaton',
    year: '1926',
    aspect: '2.39:1',
    specs: '4K UHD · 2.39:1 · TrueHD Atmos',
    synopsis:
      'Final archival plate from the Cottage Grove trestle sequence—steel, smoke, and real-time physical staging preserved at full dynamic range.',
    imageSrc: '/static/media/ribbon/red-general.jpg',
    ...SLOTS[9],
  },
];

export function getStaticCatalog(category: CorridorCategory): readonly CorridorMediaItem[] {
  return category === 'archival' ? ARCHIVAL_MEDIA : TRENDING_MEDIA;
}

/**
 * Fetches `/api/public/trending` if available and falls back to the local
 * static catalog so the landing page never blocks on unconfigured external APIs.
 */
export async function fetchCorridorCatalog(
  category: CorridorCategory,
  signal?: AbortSignal,
): Promise<readonly CorridorMediaItem[]> {
  if (category === 'archival') return ARCHIVAL_MEDIA;
  try {
    const res = await fetch('/api/public/trending', { signal });
    if (!res.ok) return TRENDING_MEDIA;
    const data = (await res.json()) as { items?: Partial<CorridorMediaItem>[] };
    if (!Array.isArray(data.items) || data.items.length === 0) return TRENDING_MEDIA;
    return data.items.slice(0, SLOTS.length).map((entry, idx) => ({
      ...TRENDING_MEDIA[idx % TRENDING_MEDIA.length],
      ...entry,
      index: idx + 1,
      ...SLOTS[idx],
    }));
  } catch {
    return TRENDING_MEDIA;
  }
}
