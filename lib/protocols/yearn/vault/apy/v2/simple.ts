import { VaultV2Contract__factory } from "@contracts/index";
import { Context } from "@data/context";
import { Apy, calculateFromPps } from "@protocols/common/apy";
import { Block, createTimedBlock, estimateBlockPrecise } from "@utils/block";
import { seconds } from "@utils/time";

import { VaultV2 } from "../../interfaces";
import { fetchHarvestCalls } from "../../reader";

function findNearestBlock(needle: Block, haystack: Block[]) {
  return haystack.reduce((a, b) =>
    Math.abs(b - needle) < Math.abs(a - needle) ? b : a
  );
}

export async function calculateSimpleApy(
  vault: VaultV2,
  ctx: Context
): Promise<Apy> {
  const contract = VaultV2Contract__factory.connect(
    vault.address,
    ctx.provider
  );
  const harvests = await fetchHarvestCalls(vault, ctx);
  if (harvests.length < 2) {
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
  const latest = await createTimedBlock(harvests[harvests.length - 1], ctx);
  const inception = await createTimedBlock(harvests[0], ctx);
  const oneWeek = await estimateBlockPrecise(
    latest.timestamp - seconds("1 week"),
    ctx
  );
  const oneMonth = await estimateBlockPrecise(
    latest.timestamp - seconds("4 weeks"),
    ctx
  );

  const oneWeekHarvest = findNearestBlock(oneWeek, harvests);
  const oneMonthHarvest = findNearestBlock(oneMonth, harvests);
  const ppsSampleData = await calculateFromPps(
    latest.block,
    inception.block,
    {
      oneWeekSample: oneWeekHarvest,
      oneMonthSample: oneMonthHarvest,
      inceptionSample: inception.block,
    },
    contract.pricePerShare
  );

  // Default to higher sample as the result is largely dependent on number of harvests (usually one week sample is sufficient)
  const netApy = Math.max(
    ppsSampleData.oneMonthSample ?? 0,
    ppsSampleData.oneWeekSample ?? 0
  );

  const v2PerformanceFee = vault.fees.general.performanceFee / 1e4;
  const v2ManagementFee = vault.fees.general.managementFee / 1e4;
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
    composite: true,
    type: "pricePerShareV2OneMonth",
    description: "Price per share - One month sample",
    data,
  };
  return apy;
}
