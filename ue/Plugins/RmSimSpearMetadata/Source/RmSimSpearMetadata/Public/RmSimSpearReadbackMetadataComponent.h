#pragma once

#include <deque>
#include <string>

#include "Components/SceneComponent.h"
#include "CoreMinimal.h"
#include "SpCore/SpFuncComponent.h"
#include "RmSimSpearReadbackMetadataComponent.generated.h"

// Companion component for a USpSceneCaptureComponent2D. It mirrors the
// capture component's enqueue/readback sequence and stores source identity in
// the SpFunc info field, so image bytes remain in SPEAR shared memory.
//
// Initialize()/Terminate() mirror USpSceneCaptureComponent2D: they are
// BlueprintCallable UFUNCTIONs so Python can drive them from inside a SPEAR
// transaction. Registering the SpFuncs there, rather than only in BeginPlay(),
// is what makes this component work when SPEAR creates it at runtime with
// unreal_service.create_scene_component_for_actor(); see Initialize().
UCLASS(ClassGroup = "rm_sim_spear", meta = (BlueprintSpawnableComponent))
class RMSIMSPEARMETADATA_API URmSimSpearReadbackMetadataComponent : public USceneComponent
{
    GENERATED_BODY()

public:
    URmSimSpearReadbackMetadataComponent();

    void BeginPlay() override;
    void EndPlay(const EEndPlayReason::Type EndPlayReason) override;

    UFUNCTION(BlueprintCallable, CallInEditor, Category = "SPEAR")
    void Initialize();

    UFUNCTION(BlueprintCallable, CallInEditor, Category = "SPEAR")
    void Terminate();

    UFUNCTION(BlueprintCallable, Category = "SPEAR")
    bool IsInitialized();

private:
    struct QueuedMetadata
    {
        std::string info;
    };

    std::string addFrameId(const std::string& info) const;

    UPROPERTY(VisibleAnywhere, Category = "SPEAR")
    USpFuncComponent* SpFuncComponent = nullptr;

    std::deque<QueuedMetadata> queued_metadata_;

    bool bInitialized = false;
};
