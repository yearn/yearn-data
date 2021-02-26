import "dotenv/config";

import { Context } from "@data/context";
import { WebSocketProvider } from "@ethersproject/providers";
import { tvl } from "@protocols/yearn/vault";

import { vaults } from "./testdata";

describe("tvl", () => {
  let provider: WebSocketProvider;
  let ctx: Context;

  beforeAll(() => {
    provider = new WebSocketProvider(process.env.WEB3_PROVIDER_WSS ?? "");
    ctx = new Context({ provider, etherscan: process.env.ETHERSCAN_API_KEY });
  });

  it("should calculate tvl for a v2 vault (network)", () => {
    const valueLocked = tvl.v2.calculateTvl(vaults.v2.object, ctx);
    return expect(valueLocked).resolves.toStrictEqual(expect.any(Number));
  }, 1e4);

  afterAll(() => {
    return provider.destroy();
  });
});
