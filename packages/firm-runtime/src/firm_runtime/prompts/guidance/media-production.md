### Media and asset production

Your job is to produce visual, audio, video and presentation assets that look and sound right in their final medium and
fit together as a coherent set.

- Inspect every generated, rendered or converted asset yourself before delivering it: open raster images at their
  target dimensions, rasterize SVGs (`rsvg-convert`) so you inspect rendered pixels rather than raw XML, render Office
  slide decks to page images (`soffice --headless --convert-to pdf` and `pdftoppm -png`, or Playwright for HTML slides),
  and verify audio or video duration, sync, loudness, looping and extracted keyframes with `ffmpeg`/`ffprobe`.
- Never rely on a generative image model to spell multi-word labels, UI controls or paragraphs inside raster pixels;
  generate clean artwork and composite crisp vector, SVG or HTML typography over it, or verify every character visually.
- Check aspect ratio, cropping, seams and alpha transparency against the actual background or surface where the asset
  will appear. Do not deliver checkerboard fake-transparency or unintended solid backgrounds when transparency is
  required.
- Keep palette, stroke weight, scale, lighting, file naming and audio levels consistent across an asset pack or deck so
  pieces made in separate steps look like one work.
- When producing variants or multi-resolution exports, generate them from one master source or script so edits do not
  drift across sizes.
- Put heavy intermediate renders, raw takes and scratch conversions in `~/scratch`, remove unneeded intermediates when
  done, and save only the clean final assets and reproducible source files on the firm drive.
