import "dotenv/config";

import { Context } from "@data/context";
import { WebSocketProvider } from "@ethersproject/providers";
import * as curve from "@protocols/curve";
import * as yearn from "@protocols/yearn";

const CurveVault = "0x5dbcF33D8c2E976c6b560249878e6F1491Bca25c";

describe("", () => {
  let provider: WebSocketProvider;
  let ctx: Context;

  beforeAll(() => {
    provider = new WebSocketProvider(process.env.WEB3_PROVIDER_WSS ?? "");
    ctx = new Context({ provider, etherscan: process.env.ETHERSCAN_API_KEY });
  });

  it("calculate apy (network)", async () => {
    const vault = await yearn.vault.resolver.resolveV1(CurveVault, ctx);
    const apy = await curve.calculateApy(vault, ctx);
    return expect(apy).toEqual({
      recommended: expect.any(Number),
      composite: expect.any(Boolean),
      type: expect.any(String),
      description: expect.any(String),
      data: expect.any(Object),
    });
  }, 3e4);

  afterAll(() => {
    return provider.destroy();
  });
});
