# Custom Fonts

Place `.ttf` or `.otf` font files here to use them in Yume subtitles.

## How to add a font

1. Download a `.ttf` or `.otf` font file
2. Place it in this `fonts/` folder
3. Open `popup.js` and add an entry to the `BUNDLED_FONTS` array:
   ```js
   const BUNDLED_FONTS = [
     { file: 'MyFont-Regular.ttf', name: 'My Font' },
   ];
   ```
4. Reload the extension in `chrome://extensions`
5. The font will appear in the subtitle font dropdowns as "My Font (bundled)"

## Recommended free fonts

| Language | Font | Source |
|----------|------|--------|
| Japanese | Noto Sans JP | [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Sans+JP) |
| Chinese (Simplified) | Noto Sans SC | [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Sans+SC) |
| Chinese (Traditional) | Noto Sans TC | [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Sans+TC) |
| Korean | Noto Sans KR | [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Sans+KR) |
| Arabic | Noto Naskh Arabic | [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Naskh+Arabic) |
| Multi-CJK | Noto Sans CJK | [GitHub](https://github.com/googlefonts/noto-cjk) |

All Noto fonts are licensed under the SIL Open Font License.
