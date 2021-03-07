import "dotenv/config";

import { Context } from "@data/context";
import { WebSocketProvider } from "@ethersproject/providers";
import { reader } from "@protocols/yearn/vault";

import { vaults } from "./testdata";

describe("yearn read vaults", () => {
  let provider: WebSocketProvider;
  let ctx: Context;

  beforeAll(() => {
    provider = new WebSocketProvider(process.env.WEB3_PROVIDER_WSS ?? "");
    ctx = new Context({ provider, etherscan: process.env.ETHERSCAN_API_KEY });
  });

  it("should find the correct inception block (network)", () => {
    const inception = reader.fetchInceptionBlock(vaults.v1.address, ctx);
    return expect(inception).resolves.toEqual({
      block: vaults.v1.inception,
      timestamp: expect.any(Number),
    });
  }, 1e4);

  it("should find the correct inception block (network)", () => {
    const inception = reader.fetchInceptionBlock(vaults.v2.address, ctx);
    return expect(inception).resolves.toEqual({
      block: vaults.v2.inception,
      timestamp: expect.any(Number),
    });
  }, 1e4);

  afterAll(() => {
    return provider.destroy();
  });
});
