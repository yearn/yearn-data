import decoder from "abi-decoder";
import path from "path";
import fs from "fs";

const abidir = path.join("abi", "full");

const [_, __, ...rest] = process.argv;

if (rest.length !== 1) {
  console.error("[!] please provide data");
  process.exit(1);
}

const data = rest[0];

function loadAbis(dir: string) {
  fs.readdirSync(dir).forEach((file) => {
    const fullpath = path.join(dir, file);
    const lstat = fs.lstatSync(fullpath);
    if (lstat.isFile() && !file.startsWith(".")) {
      try {
        const contents = fs.readFileSync(fullpath, "utf-8");
        decoder.addABI(JSON.parse(contents));
      } catch (error) {
        console.error(`Skipping ${fullpath}: ${error}`);
      }
    } else if (lstat.isDirectory()) {
      loadAbis(fullpath);
    }
  });
}

loadAbis(abidir);

console.log(decoder.decodeMethod(data));
