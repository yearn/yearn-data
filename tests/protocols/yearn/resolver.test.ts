import "dotenv/config";

import { Context } from "@data/context";
import { WebSocketProvider } from "@ethersproject/providers";
import { resolver } from "@protocols/yearn/vault";

import { vaults } from "./testdata";

describe("resolver", () => {
  let provider: WebSocketProvider;
  let ctx: Context;

  beforeAll(() => {
    provider = new WebSocketProvider(process.env.WEB3_PROVIDER_WSS ?? "");
    ctx = new Context({ provider, etherscan: process.env.ETHERSCAN_API_KEY });
  });

  it("should resolve a v1 vault (network)", async () => {
    const vault = resolver.v1.resolveVault(vaults.v1.address, ctx);
    return expect(vault).resolves.toMatchObject(vaults.v1.object);
  }, 1e4);

  it("should resolve a v2 vault (network)", () => {
    const vault = resolver.v2.resolveVault(vaults.v2.address, ctx);
    return expect(vault).resolves.toMatchObject(vaults.v2.object);
  }, 1e4);

  afterAll(() => {
    return provider.destroy();
  });
});
