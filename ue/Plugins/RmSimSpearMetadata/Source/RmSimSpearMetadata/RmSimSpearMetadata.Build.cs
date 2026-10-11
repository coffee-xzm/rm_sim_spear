using UnrealBuildTool;

public class RmSimSpearMetadata : ModuleRules
{
    public RmSimSpearMetadata(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PublicDependencyModuleNames.AddRange(new string[] { "Core", "CoreUObject", "Engine", "Json", "SpCore" });
    }
}
