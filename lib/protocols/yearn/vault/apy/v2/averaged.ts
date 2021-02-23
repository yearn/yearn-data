import { VaultV2Contract__factory } from "lib/contracts/index";
import { Context } from "lib/data/context";
import { Apy, calculateFromPps } from "lib/protocols/common/apy";
import {
  createTimedBlock,
  estimateBlockPrecise,
  fetchLatestBlock,
} from "lib/utils/block";
import { seconds } from "lib/utils/time";
import semver from "semver";

import { VaultV2 } from "../../interfaces";
import { fetchHarvestCalls } from "../../reader";

const AveragedFromVersion = "0.3.2";

export function shouldBeAveraged(vault: VaultV2): boolean {
  return semver.gte(vault.apiVersion, AveragedFromVersion);
}

export async function calculateAveraged(
  vault: VaultV2,
  ctx: Context
): Promise<Apy> {
  const contract = VaultV2Contract__factory.connect(
    vault.address,
    ctx.provider
  );
  const harvests = await fetchHarvestCalls(vault, ctx);
  if (harvests.length < 4) {
    return {
      recommended: 0,
      composite: false,
      type: "error",
      description: "no harvests",
      data: {
        oneWeekSample: null,
        oneMonthSample: null,
        inceptionSample: null,
        grossApy: null,
        netApy: null,
        performanceFee: null,
        managementFee: null,
      },
    };
  }

  const latest = await fetchLatestBlock(ctx);
  const inception = await createTimedBlock(harvests[2], ctx);
  const oneWeek = await estimateBlockPrecise(
    latest.timestamp - seconds("1 week"),
    ctx
  );
  const oneMonth = await estimateBlockPrecise(
    latest.timestamp - seconds("4 weeks"),
    ctx
  );
  const ppsSampleData = await calculateFromPps(
    latest.block,
    inception.block,
    {
      oneWeekSample: oneWeek,
      oneMonthSample: oneMonth,
      inceptionSample: inception.block,
    },
    contract.pricePerShare
  );

  const netApy =
    Math.max(ppsSampleData.oneMonthSample, ppsSampleData.oneWeekSample) || 0;
  const v2PerformanceFee = (vault.performanceFee * 2) / 10000;
  const v2ManagementFee = vault.managementFee / 10000;
  const grossApy = netApy / (1 - v2PerformanceFee) + v2ManagementFee;

  const data = {
    ...ppsSampleData,
    grossApy,
    netApy,
    performanceFee: v2PerformanceFee,
    managementFee: v2ManagementFee,
  };

  const apy = {
    recommended: grossApy,
    type: "pricePerShareV2OneMonth",
    composite: false,
    description: "Price per share - One month sample",
    data,
  };

  return apy;
}
