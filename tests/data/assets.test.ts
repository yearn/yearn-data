import { assets } from "@data/index";

describe("assets", () => {
  it("fetch aliases (network)", async () => {
    const aliases = await assets.fetchAliases();
    return expect(Object.values(aliases)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          name: expect.any(String),
          symbol: expect.any(String),
          address: expect.any(String),
        }),
      ])
    );
  }, 2e4);
  it("fetch assets (network)", async () => {
    const files = await assets.fetchAssets();
    return expect(Object.values(files)).toEqual(expect.arrayContaining([expect.any(String)]));
  }, 2e4);
});
