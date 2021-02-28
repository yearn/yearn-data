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
    "^@contracts/(.*)$": "<rootDir>/lib/contracts/$1",
    "^@data/(.*)$": "<rootDir>/lib/data/$1",
    "^@protocols/(.*)$": "<rootDir>/lib/protocols/$1",
    "^@utils/(.*)$": "<rootDir>/lib/utils/$1",
  },
};
