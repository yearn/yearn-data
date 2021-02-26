import { RegistryV1Contract__factory } from "@contracts/index";
import { Context } from "@data/context";

export const Registry = "registry.ychad.eth";

export const ExcludedVaults = ["0xec0d8D3ED5477106c6D4ea27D90a60e594693C90"];

export async function fetchAddresses(ctx: Context): Promise<string[]> {
  const registry = RegistryV1Contract__factory.connect(Registry, ctx.provider);
  const vaults = await registry.getVaults();
  return vaults.filter((address) => !ExcludedVaults.includes(address));
}
