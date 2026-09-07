// Local rendering protocol: one JSON request from stdin -> one standalone SVG result. No browser.
import { startParentGuard } from './process_guard.mjs';
startParentGuard();
let input = '';
for await (const chunk of process.stdin) input += chunk;
const request = JSON.parse(input);
let texError = null;
global.MathJax = {
  loader: {
    paths: { mathjax: '@mathjax/src/bundle' },
    load: ['input/tex', 'output/svg', 'adaptors/liteDOM'],
    require: (file) => import(file),
  },
  output: { font: 'mathjax-newcm' },
  tex: {
    packages: { '[-]': ['noundefined'] },
    formatError: (jax, error) => {
      texError = error.message;
      return jax.formatError(error);
    },
  },
  svg: { fontCache: 'none', linebreaks: { inline: false } },
};
try {
  await import('@mathjax/src/bundle/startup.js');
  await MathJax.startup.promise;
  const node = await MathJax.tex2svgPromise(request.source, {
    display: request.display !== false, em: 20, ex: 10, containerWidth: 760,
  });
  const adaptor = MathJax.startup.adaptor;
  const children = adaptor.childNodes(node).filter(child => !adaptor.kind(child).startsWith('#'));
  if (children.length !== 1 || adaptor.kind(children[0]) !== 'svg') {
    throw new Error('Expected one complete SVG; refusing to discard inline formula fragments');
  }
  const svgNode = adaptor.firstChild(node);
  // LiteDOM's HTML serializer leaves '<' in attribute values. TeX metadata is not needed
  // by SVG viewers; retain the original Markdown separately and remove that metadata.
  function stripTexMetadata(element) {
    if (adaptor.kind(element).startsWith('#')) return;
    adaptor.removeAttribute(element, 'data-latex');
    for (const child of adaptor.childNodes(element)) stripTexMetadata(child);
  }
  stripTexMetadata(svgNode);
  const fontSize = 20;
  for (const attr of ['width', 'height']) {
    const size = adaptor.getAttribute(svgNode, attr) || '';
    if (size.endsWith('ex')) adaptor.setAttribute(svgNode, attr, `${parseFloat(size) * fontSize / 2}px`);
  }
  const originalStyle = adaptor.getAttribute(svgNode, 'style') || '';
  adaptor.setAttribute(svgNode, 'color', request.dark ? '#e8edf5' : '#1c2738');
  adaptor.setAttribute(svgNode, 'style', `${originalStyle};color:${request.dark ? '#e8edf5' : '#1c2738'};font-family:Noto Sans CJK SC`);
  let svg = adaptor.outerHTML(svgNode);
  if (texError || /data-mjx-error|data-mml-node="merror"/.test(svg)) {
    throw new Error(texError || 'MathJax emitted an error node');
  }
  const style = adaptor.textContent(MathJax.startup.output.styleSheet(MathJax.startup.document));
  svg = svg.replace(/(<svg[^>]*>)/, `$1<style>${style}</style>`);
  const depth = /vertical-align:\s*(-?[\d.]+)ex/.exec(originalStyle);
  process.stdout.write(JSON.stringify({ ok: true, svg, depth_px: depth ? -parseFloat(depth[1]) * 10 : 0 }));
} catch (error) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(error.message || error) }));
  process.exitCode = 1;
} finally {
  if (global.MathJax.done) MathJax.done();
}
