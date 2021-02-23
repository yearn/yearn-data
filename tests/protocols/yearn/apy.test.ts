import "dotenv/config";

import { Context } from "@data/context";
import { WebSocketProvider } from "@ethersproject/providers";
import { calculateYearlyRoi } from "@protocols/common/apy";
import { apy, resolver } from "@protocols/yearn/vault";
import { BigNumber } from "@utils/bignumber";

import { vaults } from "./testdata";

describe("yearn yearly roi", () => {
  it("should calculate yearly roi", () => {
    expect(
      calculateYearlyRoi(
        { block: 366, value: new BigNumber(100) },
        { block: 1, value: new BigNumber(50) },
        1
      )
    ).toBe(1);
    expect(
      calculateYearlyRoi(
        { block: 366, value: new BigNumber(0) },
        { block: 1, value: new BigNumber(50) },
        1
      )
    ).toBe(-1);
    expect(
      calculateYearlyRoi(
        { block: 366, value: new BigNumber(50) },
        { block: 1, value: new BigNumber(50) },
        1
      )
    ).toBe(0);
    expect(
      calculateYearlyRoi(
        { block: 366, value: new BigNumber(75) },
        { block: 1, value: new BigNumber(50) },
        1
      )
    ).toBe(0.5);
    expect(
      calculateYearlyRoi(
        { block: 366, value: new BigNumber(25) },
        { block: 1, value: new BigNumber(50) },
        1
      )
    ).toBe(-0.5);
  });
});

describe("vault apy", () => {
  let provider: WebSocketProvider;
  let ctx: Context;

  beforeAll(() => {
    provider = new WebSocketProvider(process.env.WEB3_PROVIDER_WSS ?? "");
    ctx = new Context({ provider, etherscan: process.env.ETHERSCAN_API_KEY });
  });

  it("calculate apy v1 (network)", async () => {
    const vault = await resolver.resolveV1(vaults.v1.address, ctx);
    const inception = apy.calculateApy(vault, ctx);
    return expect(inception).resolves.toEqual({
      composite: expect.any(Boolean),
      data: {
        baseApy: expect.any(Number),
        boostedApy: expect.any(Number),
        currentBoost: expect.any(Number),
        netApy: expect.any(Number),
        keepCrv: expect.any(Number),
        poolApy: expect.any(Number),
        totalApy: expect.any(Number),
        totalPerformanceFees: expect.any(Number),
      },
      description: expect.any(String),
      recommended: expect.any(Number),
      type: expect.any(String),
    });
  }, 3e4);

  it("calculate apy v2 (network)", async () => {
    const vault = await resolver.resolveV2(vaults.v2.address, ctx);
    const inception = apy.calculateApy(vault, ctx);
    return expect(inception).resolves.toEqual({
      composite: expect.any(Boolean),
      data: {
        grossApy: expect.any(Number),
        managementFee: expect.any(Number),
        performanceFee: expect.any(Number),
        netApy: expect.any(Number),
        inceptionSample: expect.any(Number),
        oneMonthSample: expect.any(Number),
        oneWeekSample: expect.any(Number),
      },
      description: expect.any(String),
      recommended: expect.any(Number),
      type: expect.any(String),
    });
  }, 3e4);

  afterAll(() => {
    return provider.destroy();
  });
});
