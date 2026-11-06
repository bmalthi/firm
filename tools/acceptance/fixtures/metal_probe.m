#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <unistd.h>

int main(void) {
  @autoreleasepool {
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    if (!device) { fprintf(stderr, "No Metal device\n"); return 1; }
    NSError *error = nil;
    NSString *source = @"#include <metal_stdlib>\nusing namespace metal;\n"
      "kernel void compute(device uint *output [[buffer(0)]], "
      "uint i [[thread_position_in_grid]]) { output[i] = i * 3 + 7; }";
    id<MTLLibrary> library = [device newLibraryWithSource:source options:nil error:&error];
    if (!library) { NSLog(@"Shader compilation failed: %@", error); return 2; }
    id<MTLFunction> function = [library newFunctionWithName:@"compute"];
    id<MTLComputePipelineState> pipeline =
      [device newComputePipelineStateWithFunction:function error:&error];
    if (!pipeline) { NSLog(@"Pipeline failed: %@", error); return 3; }
    id<MTLBuffer> buffer = [device newBufferWithLength:256 * sizeof(uint32_t)
      options:MTLResourceStorageModeShared];
    id<MTLCommandQueue> queue = [device newCommandQueue];
    id<MTLCommandBuffer> commands = [queue commandBuffer];
    id<MTLComputeCommandEncoder> encoder = [commands computeCommandEncoder];
    [encoder setComputePipelineState:pipeline];
    [encoder setBuffer:buffer offset:0 atIndex:0];
    [encoder dispatchThreads:MTLSizeMake(256, 1, 1)
      threadsPerThreadgroup:MTLSizeMake(32, 1, 1)];
    [encoder endEncoding];
    [commands commit];
    [commands waitUntilCompleted];
    if (commands.status != MTLCommandBufferStatusCompleted) {
      NSLog(@"Execution failed: %@", commands.error); return 4;
    }
    uint32_t *values = buffer.contents;
    for (uint32_t i = 0; i < 256; i++) {
      if (values[i] != i * 3 + 7) { fprintf(stderr, "Incorrect GPU output\n"); return 5; }
    }
    NSDictionary *report = @{@"passed": @YES, @"uid": @(getuid()),
      @"device": device.name, @"checked_values": @256, @"runtime_shader_compilation": @YES};
    NSData *data = [NSJSONSerialization dataWithJSONObject:report options:0 error:&error];
    fwrite(data.bytes, 1, data.length, stdout);
    printf("\n");
  }
  return 0;
}
