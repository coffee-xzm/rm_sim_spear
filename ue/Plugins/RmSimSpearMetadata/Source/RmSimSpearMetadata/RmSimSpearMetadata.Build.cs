using UnrealBuildTool;

public class RmSimSpearMetadata : ModuleRules
{
    public RmSimSpearMetadata(ReadOnlyTargetRules Target) : base(Target)
    {
        // The metadata component includes SpCore/Boost headers whose error
        // paths use C++ exceptions, just like the main SPEAR module.
        bEnableExceptions = true;
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PublicDependencyModuleNames.AddRange(new string[] { "Core", "CoreUObject", "Engine", "Json", "SpCore" });
    }
}
