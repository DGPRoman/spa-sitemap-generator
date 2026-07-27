// Fixture site for end-to-end tests.
//
// Every link is injected by JavaScript *after* load, so a crawler that only reads
// the raw HTML source finds nothing. That is the whole point of driving a real
// browser, and this fixture is what proves the renderer actually renders.

const GRAPH = {
  "/index.html": [
    "/about.html",
    "/products/index.html",
    "/about.html", // duplicate on the same page
    "/index.html#top", // fragment-only link
    "/contact.html?utm_source=nav", // tracking query
    "https://example.com/external", // off-site
    "mailto:hi@localhost",
    "tel:+123456789",
    "javascript:void(0)",
    "/brochure.pdf", // non-HTML asset
    "/missing.html", // 404
  ],
  "/about.html": [
    "/index.html",
    "/contact.html",
    "/deep/1.html",
    "/moved.html", // 301 -> /about.html (already known)
    "/products", // 301 -> /products/index.html (missing trailing slash)
  ],
  "/products/index.html": ["/products/a.html", "/products/b.html", "/index.html"],
  "/products/a.html": ["/products/b.html", "/products/index.html"],
  "/products/b.html": ["/products/a.html"], // cycle back
  "/contact.html": ["/index.html"],
  "/deep/1.html": ["/deep/2.html"],
  "/deep/2.html": ["/deep/3.html"],
  "/deep/3.html": ["/deep/1.html"], // cycle
};

function render() {
  const path = window.location.pathname.replace(/\/$/, "/index.html");
  const links = GRAPH[path] || [];
  const app = document.getElementById("app");
  app.innerHTML =
    `<h1>${path}</h1><ul>` +
    links.map((href) => `<li><a href="${href}">${href}</a></li>`).join("") +
    "</ul>";
}

// Deliberately deferred: the DOM is empty at DOMContentLoaded.
window.addEventListener("DOMContentLoaded", () => setTimeout(render, 120));
