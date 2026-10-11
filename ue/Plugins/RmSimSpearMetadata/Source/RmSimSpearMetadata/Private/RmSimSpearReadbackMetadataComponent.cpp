#include "RmSimSpearReadbackMetadataComponent.h"

#include "CoreGlobals.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "SpCore/Unreal.h"
#include "SpCore/UnrealUtils.h"

URmSimSpearReadbackMetadataComponent::URmSimSpearReadbackMetadataComponent()
{
    PrimaryComponentTick.bCanEverTick = false;
    SpFuncComponent = UnrealUtils::createSceneComponentInsideOwnerConstructor<USpFuncComponent>(this, "sp_func_component");
    SP_ASSERT(SpFuncComponent);
}

void URmSimSpearReadbackMetadataComponent::BeginPlay()
{
    Super::BeginPlay();

    SpFuncComponent->registerFunc("bind_frame_metadata", [this](SpFuncDataBundle& args) -> SpFuncDataBundle {
        queued_metadata_.push_back({addFrameId(args.info_)});
        return SpFuncDataBundle();
    });

    SpFuncComponent->registerFunc("enqueue_copy_metadata", [this](SpFuncDataBundle& args) -> SpFuncDataBundle {
        // An empty info string marks a priming copy. Python consumes this
        // marker before validating business metadata.
        queued_metadata_.push_back({addFrameId(args.info_)});
        return SpFuncDataBundle();
    });

    SpFuncComponent->registerFunc("readback_metadata", [this](SpFuncDataBundle&) -> SpFuncDataBundle {
        SpFuncDataBundle result;
        if (queued_metadata_.empty()) {
            return result;
        }
        QueuedMetadata queued = queued_metadata_.front();
        queued_metadata_.pop_front();
        if (!queued.info.empty()) {
            result.info_ = queued.info;
        }
        return result;
    });

    SpFuncComponent->registerFunc("reset_metadata", [this](SpFuncDataBundle&) -> SpFuncDataBundle {
        queued_metadata_.clear();
        return SpFuncDataBundle();
    });
}

void URmSimSpearReadbackMetadataComponent::EndPlay(const EEndPlayReason::Type EndPlayReason)
{
    SpFuncComponent->unregisterFunc("bind_frame_metadata");
    SpFuncComponent->unregisterFunc("enqueue_copy_metadata");
    SpFuncComponent->unregisterFunc("readback_metadata");
    SpFuncComponent->unregisterFunc("reset_metadata");
    queued_metadata_.clear();
    Super::EndPlay(EndPlayReason);
}

std::string URmSimSpearReadbackMetadataComponent::addFrameId(const std::string& info) const
{
    if (info.empty()) {
        return "";
    }

    TSharedPtr<FJsonObject> object;
    TSharedRef<TJsonReader<>> reader = TJsonReaderFactory<>::Create(Unreal::toFString(info));
    if (!FJsonSerializer::Deserialize(reader, object) || !object.IsValid()) {
        return "";
    }
    object->SetNumberField(FStringView(TEXT("ue_frame_id")), static_cast<double>(GFrameCounter));

    FString serialized;
    TSharedRef<TJsonWriter<>> writer = TJsonWriterFactory<>::Create(&serialized);
    if (!FJsonSerializer::Serialize(object.ToSharedRef(), writer)) {
        return "";
    }
    return Unreal::toStdString(serialized);
}
