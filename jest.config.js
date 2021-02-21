const { resolve } = require("path");

module.exports = {
  clearMocks: true,
  moduleFileExtensions: ["js", "jsx", "ts", "tsx"],
  roots: ["<rootDir>/tests"],
  testEnvironment: "node",
  transform: {
    "^.+\\.tsx?$": "ts-jest",
  },
  moduleNameMapper: {
    "^@contracts/(.*)$": resolve(__dirname, "./lib/sdk/contracts/$1"),
    "^@data/(.*)$": resolve(__dirname, "./lib/sdk/data/$1"),
    "^@protocols/(.*)$": resolve(__dirname, "./lib/sdk/protocols/$1"),
    "^@utils/(.*)$": resolve(__dirname, "./lib/sdk/utils/$1"),
  },
};
