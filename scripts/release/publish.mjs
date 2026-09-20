import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Invoked by @semantic-release/exec as the publishCmd. It reads the release computed
// by semantic-release from its arguments (semantic-release only interpolates the command
// string, it does not export nextRelease.* as environment variables) and builds + pushes
// the multi-arch GHCR image whose input is the matching JAR asset of IMXEren/Suwayomi-Server.
//
// Stable (channel unset)   -> tags: latest, stable, <version>
// Prerelease (channel=dev) -> tags: dev, <version>

const GITHUB_API = "https://api.github.com";
const SOURCE_REPO = process.env.SUWAYOMI_SOURCE_REPO || "IMXEren/Suwayomi-Server";
const PLATFORMS = process.env.RELEASE_PLATFORMS || "linux/amd64,linux/arm64";

function run(cmd, args) {
  console.log(`$ ${cmd} ${args.join(" ")}`);
  execFileSync(cmd, args, { stdio: "inherit" });
}

async function ghJson(url) {
  const headers = {
    accept: "application/vnd.github+json",
    "user-agent": "imxeren-suwayomi-release",
  };
  if (process.env.GITHUB_TOKEN) headers.authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
  const res = await fetch(url, { headers });
  if (!res.ok) throw new Error(`GET ${url} -> ${res.status} ${res.statusText}`);
  return res.json();
}

async function pickJarRelease(isPrerelease) {
  const releases = await ghJson(`${GITHUB_API}/repos/${SOURCE_REPO}/releases?per_page=30`);
  const withJar = releases.filter((r) => Array.isArray(r.assets) && r.assets.some((a) => /\.jar$/i.test(a.name)));
  if (withJar.length === 0) throw new Error(`no release with a JAR asset found for ${SOURCE_REPO}`);

  const wanted = isPrerelease
    ? withJar.find((r) => r.prerelease || r.tag_name.includes("-"))
    : withJar.find((r) => !r.prerelease);

  // A stable image can only be built from a stable JAR. Until the source repository has
  // published one, fall back to the newest prerelease instead of failing the release.
  const chosen = wanted || withJar[0];
  if (!wanted && !isPrerelease) {
    console.warn(`no stable release with a JAR asset found for ${SOURCE_REPO}; falling back to ${chosen.tag_name}`);
  }

  const asset = chosen.assets.find((a) => /\.jar$/i.test(a.name));
  return { tag: chosen.tag_name, url: asset.browser_download_url, filename: asset.name };
}

async function detectJbrTag(jarUrl) {
  const dir = mkdtempSync(join(tmpdir(), "swy-jbr-"));
  try {
    const jar = join(dir, "server.jar");
    const res = await fetch(jarUrl);
    if (!res.ok) throw new Error(`GET ${jarUrl} -> ${res.status}`);
    writeFileSync(jar, Buffer.from(await res.arrayBuffer()));
    execFileSync("unzip", ["-o", "-q", jar, "META-INF/MANIFEST.MF", "-d", dir], { stdio: "inherit" });
    const manifest = readFileSync(join(dir, "META-INF", "MANIFEST.MF"), "utf8");
    const match = manifest.match(/X-JBR-Release:\s*(\S+)/);
    return match ? match[1] : "";
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

// semantic-release interpolates these into the publishCmd string; the environment fallback
// exists so the script can also be exercised directly.
function normalize(value) {
  const text = (value ?? "").trim();
  return !text || text === "undefined" || text === "null" ? "" : text;
}

const version = normalize(process.argv[2] || process.env.RELEASE_VERSION);
if (!version) {
  throw new Error(
    "no release version was passed; publish.mjs must run through semantic-release with " +
      "nextRelease.version interpolated into the publishCmd",
  );
}
const channel = normalize(process.argv[3] || process.env.RELEASE_CHANNEL);
const isPrerelease = channel !== "";
const sourceBranch = process.env.SUWAYOMI_SOURCE_BRANCH || (isPrerelease ? "dev" : "main");

const owner = (process.env.GITHUB_REPOSITORY_OWNER || "IMXEren").toLowerCase();
const image = process.env.IMAGE_NAME || `ghcr.io/${owner}/suwayomi-server`;

const release = await pickJarRelease(isPrerelease);
const jbrTag = await detectJbrTag(release.url).catch((err) => {
  console.warn(`could not read the JBR release from the JAR manifest: ${err.message}`);
  return "";
});

const tags = isPrerelease
  ? [`${image}:dev`, `${image}:${version}`]
  : [`${image}:latest`, `${image}:stable`, `${image}:${version}`];

const buildArgs = [
  `--build-arg=BUILD_DATE=${new Date().toISOString().slice(0, 10)}`,
  `--build-arg=TACHIDESK_RELEASE_TAG=${release.tag}`,
  `--build-arg=TACHIDESK_RELEASE_DOWNLOAD_URL=${release.url}`,
  `--build-arg=TACHIDESK_FILENAME=${release.filename}`,
  `--build-arg=TACHIDESK_DOCKER_GIT_COMMIT=${process.env.GITHUB_RUN_NUMBER || ""}`,
  // empty value = auto-detect KCEF from the target platform (amd64/arm64 build it in)
  `--build-arg=TACHIDESK_KCEF=`,
  `--build-arg=TACHIDESK_ABORT_HANDLER_DOWNLOAD_URL=https://raw.githubusercontent.com/${SOURCE_REPO}/refs/heads/${sourceBranch}/scripts/resources/catch_abort.c`,
];
if (jbrTag) {
  buildArgs.push(`--build-arg=TACHIDESK_KCEF_RELEASE_URL=${GITHUB_API}/repos/JetBrains/JetBrainsRuntime/releases/tags/${jbrTag}`);
}

console.log(`building ${image} for ${PLATFORMS} (channel=${channel || "stable"}, source tag=${release.tag})`);

if (normalize(process.env.RELEASE_DRY_RUN)) {
  console.log(`dry run: docker buildx build --platform ${PLATFORMS} ${[...buildArgs, ...tags.flatMap((tag) => ["-t", tag]), "--push"].join(" ")} .`);
  process.exit(0);
}

run("docker", [
  "buildx",
  "build",
  "--platform",
  PLATFORMS,
  "--provenance=true",
  "--sbom=true",
  ...buildArgs,
  ...tags.flatMap((tag) => ["-t", tag]),
  "--push",
  ".",
]);
console.log(`pushed: ${tags.join(", ")}`);
